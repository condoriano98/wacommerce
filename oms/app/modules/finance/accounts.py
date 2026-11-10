"""Chart of accounts. Codes are referenced by posting rules; never renumber."""

# code: (name, type, is_cash, is_working_capital)
ACCOUNTS: dict[str, tuple[str, str, bool, bool]] = {
    "1000": ("Cash and bank", "asset", True, False),
    "1010": ("Payment gateway clearing", "asset", False, True),
    "1100": ("COD receivable (courier)", "asset", False, True),
    "1110": ("Marketplace receivable", "asset", False, True),
    "1120": ("Supplier refund receivable", "asset", False, True),
    "1200": ("Inventory", "asset", False, True),
    "1210": ("Supplier prepayments (dropship in transit)", "asset", False, True),
    "1300": ("Customer advances (unearned revenue)", "liability", False, True),
    "2000": ("Supplier payables", "liability", False, True),
    "2100": ("Customer refunds payable", "liability", False, True),
    "3000": ("Owner capital", "equity", False, False),
    "3100": ("Inter-portfolio capital", "equity", False, False),
    "4000": ("Sales revenue", "revenue", False, False),
    "4100": ("Shipping income", "revenue", False, False),
    "4900": ("Sales returns and allowances", "revenue", False, False),
    "5000": ("Cost of goods sold", "expense", False, False),
    "5100": ("Shipping expense", "expense", False, False),
    "5200": ("Payment and COD fees", "expense", False, False),
    "5300": ("Marketplace fees", "expense", False, False),
    "5400": ("Packing", "expense", False, False),
    "5500": ("Return and failed-delivery losses", "expense", False, False),
    "6000": ("Advertising", "expense", False, False),
    "6900": ("Operating expenses", "expense", False, False),
}

CASH = "1000"
GATEWAY_CLEARING = "1010"
COD_RECEIVABLE = "1100"
MARKETPLACE_RECEIVABLE = "1110"
SUPPLIER_REFUND_RECEIVABLE = "1120"
INVENTORY = "1200"
SUPPLIER_PREPAYMENTS = "1210"
CUSTOMER_ADVANCES = "1300"
SUPPLIER_PAYABLES = "2000"
REFUNDS_PAYABLE = "2100"
OWNER_CAPITAL = "3000"
INTER_PORTFOLIO_CAPITAL = "3100"
REVENUE = "4000"
SHIPPING_INCOME = "4100"
SALES_RETURNS = "4900"
COGS = "5000"
SHIPPING_EXPENSE = "5100"
PAYMENT_FEES = "5200"
MARKETPLACE_FEES = "5300"
PACKING = "5400"
RETURN_LOSSES = "5500"
ADVERTISING = "6000"
OPEX = "6900"
