SUPPORTED_CURRENCIES = [
    "VND",
]

DEFAULT_PAYMENT_METHODS_CODES = [
    "mbbank",
]

# MB Bank API Endpoints
SANDBOX_DOMAIN = "https://api-sandbox.mbbank.com.vn"
PRODUCTION_DOMAIN = "https://api.mbbank.com.vn"
CREATE_ORDER_PATH = "/private/ms/pg-paygate-authen/paygate/v2/create-order"
QUERY_STATUS_PATH = "/private/ms/pg-paygate-authen/v2/paygate/detail"
REFUND_PATH = "/private/ms/pg-paygate-authen/paygate/refund/single"


# MB Bank Payment Methods
PAYMENT_METHOD_QR = "QR"
PAYMENT_METHOD_ATM = "ATMCARD"

# MB Bank Payment Languages
PAYMENT_LANG_VI = "vi"
PAYMENT_LANG_EN = "en"

# MB Bank Error Codes
ERROR_CODE_SUCCESS = "00"
ERROR_CODE_PENDING = "12"
ERROR_CODE_CANCELED = "18"