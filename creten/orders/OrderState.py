class OrderState:
	OPEN_PENDING_INT = 1
	OPEN_PENDING_EXT = 2
	OPEN_FAILED = 3
	OPENED = 4
	PARTIALLY_FILLED = 5
	FILLED = 6
	CANCEL_PENDING_INT = 7
	CANCEL_PENDING_EXT = 8
	CANCEL_FAILED = 9
	CANCELED = 10
	REJECTED = 11
	EXPIRED = 12