from colorama import Fore, Back, Style
from datetime import datetime
from decimal import Decimal, ROUND_DOWN
from common.Db import Db
from common.Settings import Settings
from db_managers.TradeManager import TradeManager
from db_managers.DbCodeMapper import OrderTypeMapper, OrderStateMapper, TradeTypeMapper, OrderSideMapper, TradeStateMapper
from db_entities.Trade import Trade
from db_entities.Orders import Orders
from orders.TradeType import TradeType
from orders.TradeState import TradeState
from orders.OrderSide import OrderSide
from orders.OrderState import OrderState
from orders.OrderType import OrderType
from common.Logger import Logger
from orders.Order import Order
from orders.TradeCloseStrategy import TradeCloseStrategy
import orders.Trade

class OrderManager(object):
	def __init__(self, exchangeClient, marketRulesManager, strategyManager):
		self.exchangeClient = exchangeClient
		self.marketRulesManager = marketRulesManager
		self.strategyManager = strategyManager

		self.shapeNewOrders = True
		self.validateOrders = True
		self.preventImmediateLimitOrder = True

		self.liveOrderCache = {}
		self.liveTradeCache = {}

		self.log = Logger()

	def setShapeNewOrders(self, shapeNewOrders):
		self.shapeNewOrders = shapeNewOrders

	def setValidateOrders(self, validateOrders):
		self.validateOrders = validateOrders

	def setPreventImmediateLimitOrder(self, preventImmediateLimitOrder):
		self.preventImmediateLimitOrder = preventImmediateLimitOrder

	def getLiveOrderCache(self):
		return self.liveOrderCache

	def getLiveTradeCache(self):
		return self.liveTradeCache

	def reset(self):
		self.liveTradeCache = {}
		self.liveOrderCache = {}

	def storeOrder(self, order):
		Db.Session().add(order)

		# flush in order to generate db maintained sequence
		Db.Session().flush()

		# update internal reference only when we got new order_id
		if not order.int_order_ref:
			order.int_order_ref = Settings.ORDER_REFERENCE_PREFIX + str(order.order_id)

		# track order updates in the cache.
		o = Order()
		o.setFromEntity(order)
		self.liveOrderCache[order.order_id] = o

	def _storeTrade(self, trade):
		Db.Session().add(trade)

		# flush in order to generate db maintained sequence
		Db.Session().flush()

		# track trade updates in the cache. if trade reached a final state, remove it from the cache
		t = orders.Trade.Trade()
		t.setFromEntity(trade)
		if not trade.trade_id in self.liveTradeCache:
			self.liveTradeCache[trade.trade_id] = t
		else:
			if trade.trade_state in TradeStateMapper.getDbValue([TradeState.CLOSED, TradeState.CLOSE_FAILED, TradeState.OPEN_FAILED]):
				self.liveTradeCache.pop(trade.trade_id, None)

				# once trade is removed, clean also cache of corresponding trade orders
				for orderKey in list(self.liveOrderCache.keys()):
					if self.liveOrderCache[orderKey].getTradeId() == t.getTradeId():
						self.liveOrderCache.pop(orderKey)
			else:
				self.liveTradeCache[trade.trade_id] = t

	def closeTrade(self, trade, closeTmstmp):
		self.log.info('Trade ' + str(trade.trade_id) + ' CLOSED.')
		trade.trade_state = TradeStateMapper.getDbValue(TradeState.CLOSED)
		trade.close_tmstmp = closeTmstmp

		# notify strategy about the closed trade
		self.strategyManager.getStrategy(trade.strategy_exec_id).tradeClosed(trade.trade_id)

		self._calcTradePerf(trade)

	def _calcTradePerf(self, trade):
		orders = TradeManager.getAllOrders(tradeId = trade.trade_id, orderState = OrderStateMapper.getDbValue(OrderState.FILLED))

		buyPrice = Decimal(0.0)
		sellPrice = Decimal(0.0)
		for order in orders:
			if order.order_side == OrderSideMapper.getDbValue(OrderSide.BUY):
				buyPrice += order.qty * Decimal(order.price)
			else:
				sellPrice += order.qty * Decimal(order.price)

		diff = sellPrice - buyPrice

		rules = self.marketRulesManager.getSymbolRules(trade.base_asset, trade.quote_asset)
		if diff < 0:
			color = Style.BRIGHT + Fore.LIGHTWHITE_EX + Back.RED
		else:
			color = Style.BRIGHT + Fore.LIGHTWHITE_EX + Back.GREEN
		self.log.info(color + 'Gain: ' + str(('{:.' + str(rules.quoteAssetPrecision) + 'f}').format(diff)) + Style.RESET_ALL)

	def processOrderUpdate(self, orderResponse):
		# update order in the database based on the update response
		order = TradeManager.getOrder(intOrderRef = orderResponse.getClientOrderId())
		order.order_state = OrderStateMapper.getDbValue(orderResponse.getOrderState())
		order.lst_upd_tmstmp = datetime.now()
		if orderResponse.getOrderState() == OrderState.OPENED:
			order.open_tmstmp = orderResponse.getOrderTmstmp()
		if orderResponse.getOrderState() == OrderState.FILLED:
			self.log.debug('Order ' + str(order.order_id) + ' FILLED.')
			order.filled_tmstmp = orderResponse.getOrderTmstmp()
		if orderResponse.getOrderState() == OrderState.CANCELED:
			self.log.debug('Order ' + str(order.order_id) + ' CANCELLED.')

		# update trade in the database based on the state of orders
		trade = TradeManager.getTrade(tradeId = order.trade_id)
		if trade.trade_type == TradeTypeMapper.getDbValue(TradeType.LONG):
			# open trade when first confirmed BUY is received
			if orderResponse.getOrderSide() == OrderSide.BUY and \
					orderResponse.getOrderState() == OrderState.OPENED and \
					trade.trade_state == TradeStateMapper.getDbValue(TradeState.OPEN_PENDING):
				trade.trade_state = TradeStateMapper.getDbValue(TradeState.OPENED)
				trade.open_tmstmp = orderResponse.getOrderTmstmp()

			# close trade when no pending order is left
			elif orderResponse.getOrderState() in [OrderState.CANCELED, OrderState.REJECTED, OrderState.EXPIRED]:
				openOrders = TradeManager.getPendOrders(trade.trade_id)
				if len(openOrders) == 0:
					self.closeTrade(trade, orderResponse.getOrderTmstmp())

			# handle closing of the trade based on the defined strategy
			elif orderResponse.getOrderState() == OrderState.FILLED:
				tradeCloseType = self.strategyManager.getStrategy(trade.strategy_exec_id).getTradeCloseType()
				TradeCloseStrategy.evalTradeClose(tradeCloseType, trade, orderResponse, self)
		else:
			raise Exception('Short trades not supported!')

		self.storeOrder(order)
		self._storeTrade(trade)

	def _validateOrders(self, baseAsset, quoteAsset, orders):
		rules = self.marketRulesManager.getSymbolRules(baseAsset, quoteAsset)

		for order in orders:
			qty = order.getQty()
			if qty < rules.minQty:
				raise Exception('Quantity is less than minimum quantity! Qty [' + str(qty) + '], minQty [' + str(rules.minQty) + ']')

			if qty > rules.maxQty:
				raise Exception('Quantity is greater than maximum quantity! Qty [' + str(qty) + '], maxQty [' + str(rules.maxQty) + ']')

			if (qty - rules.minQty) % rules.minQtyDenom != 0:
				raise Exception('Quantity is not multiply of denomination! qty [' + str(qty) + '], minQty [' + str(rules.minQty) + '],  minDenom [' + str(rules.minQtyDenom) + ']')

			price = order.getPrice()
			if price:
				if price < rules.minPrice:
					raise Exception('Price is less than minimum price! Price [' + str(price) + '], minPrice [' + str(rules.minPrice) + ']')

				if price > rules.maxPrice:
					raise Exception('Price is greater than maximum price! Price [' + str(price) + '], maxPrice [' + str(rules.maxPrice) + ']')

				if (price - rules.minPrice) % rules.minPriceDenom != 0:
					raise Exception('Price is not multiply of denomination! Price [' + str(price) + '], minPrice [' + str(rules.minPrice) + '],  minDenom [' + str(rules.minPriceDenom) + ']')

				if price * qty < rules.minNotional:
					raise Exception('Quantity and price is less than minimum notional value! Qty [' + str(qty) + '], price [' + str(price) + '],  minNotional [' + str(rules.minNotional) + ']')

			stopPrice = order.getStopPrice()
			if stopPrice:
				if stopPrice < rules.minPrice:
					raise Exception('Stop price is less than minimum price! Stop price [' + str(stopPrice) + '], minPrice [' + str(rules.minPrice) + ']')

				if stopPrice > rules.maxPrice:
					raise Exception('Stop price is greater than maximum price! Stop price [' + str(stopPrice) + '], maxPrice [' + str(rules.maxPrice) + ']')

				if (stopPrice - rules.minPrice) % rules.minPriceDenom != 0:
					raise Exception('Stop price is not multiply of denomination! Stop price [' + str(stopPrice) + '], minPrice [' + str(rules.minPrice) + '],  minDenom [' + str(rules.minPriceDenom) + ']')

				if stopPrice * qty < rules.minNotional:
					raise Exception('Quantity and stop price is less than minimum notional value! Qty [' + str(qty) + '], stop price [' + str(stopPrice) + '],  minNotional [' + str(rules.minNotional) + ']')

		return True

	def _validateImmediateLimitOrder(self, candle, orders):
		for order in orders:
			if order.getOrderSide() == OrderSide.BUY:
				if order.getOrderType() == OrderType.LIMIT and order.getPrice() >= candle.getClose():
					raise Exception("LIMIT BUY order will be executed immediately! Order price [" + str(order.getPrice()) + "], market price [" + str(candle.getClose()) + "]")
				if order.getOrderType() in [OrderType.STOP_LOSS_LIMIT, OrderType.STOP_LOSS_MARKET] and order.getStopPrice() <= candle.getClose():
					raise Exception("STOP LOSS BUY order will be executed immediately! Order stop price [" + str(order.getStopPrice()) + "], market price [" + str(candle.getClose()) + "]")
				if order.getOrderType() in [OrderType.TAKE_PROFIT_LIMIT, OrderType.TAKE_PROFIT_MARKET] and order.getStopPrice() >= candle.getClose():
					raise Exception("TAKE PROFIT BUY order will be executed immediately! Order stop price [" + str(order.getStopPrice()) + "], market price [" + str(candle.getClose()) + "]")

			if order.getOrderSide() == OrderSide.SELL:
				if order.getOrderType() == OrderType.LIMIT and order.getPrice() <= candle.getClose():
					raise Exception("LIMIT SELL order will be executed immediately! Order price [" + str(order.getPrice()) + "], market price [" + str(candle.getClose()) + "]")
				if order.getOrderType() in [OrderType.STOP_LOSS_LIMIT, OrderType.STOP_LOSS_MARKET] and order.getStopPrice() >= candle.getClose():
					raise Exception("STOP LOSS SELL order will be executed immediately! Order stop price [" + str(order.getStopPrice()) + "], market price [" + str(candle.getClose()) + "]")
				if order.getOrderType() in [OrderType.TAKE_PROFIT_LIMIT, OrderType.TAKE_PROFIT_MARKET] and order.getStopPrice() <= candle.getClose():
					raise Exception("TAKE PROFIT SELL order will be executed immediately! Order stop price [" + str(order.getStopPrice()) + "], market price [" + str(candle.getClose()) + "]")

	def _shapeValue(self, value, minVal, minDenom, precision):
		self.log.debug('Shaping value [' + str(value) + '], minVal [' + str(minVal) + '], minDenom [' + str(minDenom) + '], precision [' + str(precision) + ']')

		ret = Decimal(minVal) + Decimal(minDenom) * Decimal((Decimal(value) - Decimal(minVal)) / Decimal(minDenom)).quantize(Decimal('1'), rounding = ROUND_DOWN)
		#ret = Decimal(ret).quantize(Decimal('10') ** -precision)

		self.log.debug('Shaped value [' + str(ret) + ']')

		return ret

	def _shapeOrders(self, baseAsset, quoteAsset, orders):
		rules = self.marketRulesManager.getSymbolRules(baseAsset, quoteAsset)

		for order in orders:
			if order.getQty():
				self.log.debug('Shaping quantity')
				order.setQty(self._shapeValue(order.getQty(), rules.minQty, rules.minQtyDenom, rules.baseAssetPrecision))
			if order.getPrice():
				self.log.debug('Shaping price')
				order.setPrice(self._shapeValue(order.getPrice(), rules.minPrice, rules.minPriceDenom, rules.quoteAssetPrecision))
			if order.getStopPrice():
				self.log.debug('Shaping stop price')
				order.setStopPrice(self._shapeValue(order.getStopPrice(), rules.minPrice, rules.minPriceDenom, rules.quoteAssetPrecision))

	def openTrade(self, strategyExecId, tradeType, candle):
		trade = Trade()
		trade.strategy_exec_id = strategyExecId
		trade.base_asset = candle.getBaseAsset()
		trade.quote_asset = candle.getQuoteAsset()
		trade.init_tmstmp = candle.getCloseTime()
		trade.trade_state = TradeStateMapper.getDbValue(TradeState.OPEN_PENDING)
		trade.trade_type = TradeTypeMapper.getDbValue(tradeType)

		self._storeTrade(trade)

		return trade

	def openOrder(self, trade, candle, orders):
		self.log.debug('Orders to be created: ' + str(len(orders)))

		# adjust quantity and price such that it meets market rules
		if self.shapeNewOrders:
			self._shapeOrders(candle.getBaseAsset(), candle.getQuoteAsset(), orders)

		# validate orders before sending them to the exchange
		if self.validateOrders:
			self._validateOrders(candle.getBaseAsset(), candle.getQuoteAsset(), orders)

		# make sure that limit orders are not executed immediately
		if self.preventImmediateLimitOrder:
			self._validateImmediateLimitOrder(candle, orders)

		for order in orders:
			try:
				dbOrder = Orders()
				dbOrder.trade_id = trade.trade_id
				dbOrder.qty = order.getQty()
				dbOrder.stop_price = order.getStopPrice()
				if order.getOrderType() == OrderType.MARKET:
					dbOrder.price = candle.getClose()
				else:
					dbOrder.price = order.getPrice()
				dbOrder.order_side = OrderSideMapper.getDbValue(order.getOrderSide())
				dbOrder.order_type = OrderTypeMapper.getDbValue(order.getOrderType())
				dbOrder.order_state = OrderStateMapper.getDbValue(OrderState.OPEN_PENDING_INT)
				dbOrder.init_tmstmp = candle.getCloseTime()

				self.storeOrder(dbOrder)

				# save the generated order id
				order.setOrderId(dbOrder.order_id)
				# save the order state (for print)
				order.setOrderState(OrderState.OPEN_PENDING_INT)

				self.log.info('Order: ' + str(order))
			except:
				self.log.error('Could not create order ' + str(order))
				raise

	def sendOrders(self, cretenExecDetlId):
		ordersToBeSent = TradeManager.getAllOrders(cretenExecDetlId = cretenExecDetlId, orderState = OrderStateMapper.getDbValue([OrderState.OPEN_PENDING_INT, OrderState.CANCEL_PENDING_INT]))

		for dbOrder in ordersToBeSent:
			if dbOrder.order_state == OrderStateMapper.getDbValue(OrderState.OPEN_PENDING_INT):
				try:
					order = Order()
					order.setFromEntity(dbOrder)

					self.log.debug('Processing order [' + str(order) + ']')

					trade = TradeManager.getTrade(tradeId = dbOrder.trade_id)

					rules = self.marketRulesManager.getSymbolRules(trade.base_asset, trade.quote_asset)

					qty = ('{:.' + str(rules.baseAssetPrecision) + 'f}').format(order.getQty())
					price = ('{:.' + str(rules.quoteAssetPrecision) + 'f}').format(order.getPrice()) if order.getPrice() else None
					stopPrice = ('{:.' + str(rules.quoteAssetPrecision) + 'f}').format(order.getStopPrice()) if order.getStopPrice() else None
					self.log.debug('Values to be sent: qty [' + str(qty) + '], price [' + str(price) + '], stop price [' + str(stopPrice) + ']')

					response = self.exchangeClient.createOrder(order.getOrderSide(), order.getOrderType(),
					                                           trade.base_asset, trade.quote_asset, qty,
					                                            stopPrice, price, order.getIntOrderRef())

					self.log.debug('Response: ' + str(response.getRawData()))

					if not response.getOrderState() in [OrderState.OPENED, OrderState.FILLED]:
						raise Exception('Unexpected response received for order [' + str(order) + ']! Expected state [' + str([OrderState.OPENED, OrderState.FILLED]) + '], received [' + str(response.getOrderState()) + ']')

					if order.getOrderType() == OrderType.MARKET:
						dbOrder.price = response.getPrice()
					dbOrder.ext_order_ref = response.getExtOrderRef()
					dbOrder.order_state = OrderStateMapper.getDbValue(OrderState.OPEN_PENDING_EXT)
					self.storeOrder(dbOrder)
				except:
					self.log.error('Error while creating orders!')
					dbOrder.order_state = OrderStateMapper.getDbValue(OrderState.OPEN_FAILED)
					self.storeOrder(dbOrder)

					raise
			elif dbOrder.order_state == OrderStateMapper.getDbValue(OrderState.CANCEL_PENDING_INT):
				try:
					order = Order()
					order.setFromEntity(dbOrder)

					self.log.debug('Cancelling order [' + str(order) + ']')

					trade = TradeManager.getTrade(tradeId = dbOrder.trade_id)
					response = self.exchangeClient.cancelOrder(baseAsset = trade.base_asset, quoteAsset = trade.quote_asset, clientOrderId = order.getIntOrderRef())

					self.log.debug('Response: ' + str(response.getRawData()))

					if not response.getOrderState() in [OrderState.OPENED]:
						raise Exception('Unexpected response received for order [' + str(order) + ']! Expected state [' + str([OrderState.OPENED, OrderState.FILLED]) + '], received [' + str(response.getOrderState()) + ']')

					dbOrder.ext_order_ref = response.getExtOrderRef()
					dbOrder.order_state = OrderStateMapper.getDbValue(OrderState.CANCEL_PENDING_EXT)
					self.storeOrder(dbOrder)
				except:
					self.log.error('Error while cancelling orders!')
					dbOrder.order_state = OrderStateMapper.getDbValue(OrderState.CANCEL_FAILED)
					self.storeOrder(dbOrder)

					raise
			else:
				raise Exception('Creating orders for unsupporterd order state!')

	# TODO
	def init(self):
		pass