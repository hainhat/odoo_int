SUPPORTED_CURRENCIES = [
    "VND",
]

DEFAULT_PAYMENT_METHODS_CODES = [
    "momo",
]

# MoMo API Endpoints
SANDBOX_DOMAIN = "https://test-payment.momo.vn"
PRODUCTION_DOMAIN = "https://payment.momo.vn"
CREATE_PAYMENT_PATH = "/v2/gateway/api/create"
CHECK_STATUS_PATH = "/v2/gateway/api/query"

# MoMo Request Types
REQUEST_TYPE_CAPTURE_WALLET = "captureWallet"
REQUEST_TYPE_PAY_WITH_METHOD = "payWithMethod"

# MoMo Payment Languages
PAYMENT_LANG_VI = "vi"
PAYMENT_LANG_EN = "en"

# MoMo Result Codes
RESULT_CODE_SUCCESS = 0
RESULT_CODE_AUTHORIZED = 9000
RESULT_CODE_PENDING = 1000

# Transaction Status Values
TRANSACTION_STATUS_PENDING = 0
TRANSACTION_STATUS_SUCCESS = 1
TRANSACTION_STATUS_FAILED = 2