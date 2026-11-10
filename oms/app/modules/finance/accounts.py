"""Chart of accounts. Codes are referenced by posting rules; never renumber."""

# code: (name, type, is_cash, is_working_capital)
ACCOUNTS: dict[str, tuple[str, str, bool, bool]] = {
    "1000": ("Cash and bank", "asset", True, False),
    "1010": ("Payment gateway clearing", "asset", False, True),
    # Provider-reported payouts not yet proven by a bank statement line; never part of verified cash (remediation B5).
    "1050": ("Funds in transit to bank (unverified payouts)", "asset", False, True),
    "1020": ("Carrier balance (Biteship)", "asset", False, True),
    "1030": ("Prepaid ad balances", "asset", False, True),
    "1100": ("COD receivable (courier)", "asset", False, True),
    "1105": ("Customer receivable - transfer pending verification", "asset", False, True),
    "1109": ("Allowance for expected credit losses", "asset", False, True),
    "1110": ("Marketplace receivable", "asset", False, True),
    "1120": ("Supplier refund receivable", "asset", False, True),
    "1200": ("Inventory", "asset", False, True),
    "1210": ("Supplier prepayments (dropship in transit)", "asset", False, True),
    "1250": ("Goods in transit", "asset", False, True),
    "1300": ("Customer advances (V3.0, inactive - use 2200)", "liability", False, True),
    "1400": ("Prepaid taxes (PPh 22 creditable)", "asset", False, False),
    "1900": ("Intercompany receivable", "asset", False, False),
    "2000": ("Supplier payables", "liability", False, True),
    "2010": ("Carrier payables", "liability", False, True),
    "2020": ("Ads payables", "liability", False, True),
    "2100": ("Customer refunds payable", "liability", False, True),
    "2200": ("Contract liabilities (customer advances)", "liability", False, True),
    "2300": ("Taxes payable", "liability", False, False),
    "2900": ("Intercompany payable", "liability", False, False),
    "3000": ("Owner capital", "equity", False, False),
    "3100": ("Inter-portfolio capital (V3.0, inactive)", "equity", False, False),
    "3900": ("Retained earnings", "equity", False, False),
    "4000": ("Sales revenue", "revenue", False, False),
    "4100": ("Shipping income", "revenue", False, False),
    "4900": ("Sales returns and allowances", "revenue", False, False),
    "5000": ("Cost of goods sold", "expense", False, False),
    "5100": ("Shipping expense", "expense", False, False),
    "5200": ("Payment and COD fees", "expense", False, False),
    "5300": ("Marketplace fees", "expense", False, False),
    "5400": ("Packing", "expense", False, False),
    "5500": ("Return and failed-delivery losses", "expense", False, False),
    "5600": ("Variable AI and customer-service cost", "expense", False, False),
    "5700": ("Expected credit losses (net of recoveries)", "expense", False, False),
    "6000": ("Advertising", "expense", False, False),
    "6900": ("Operating expenses", "expense", False, False),
}

INACTIVE = {"1300", "3100"}

CASH = "1000"
GATEWAY_CLEARING = "1010"
FUNDS_IN_TRANSIT = "1050"
CARRIER_BALANCE = "1020"
PREPAID_ADS = "1030"
COD_RECEIVABLE = "1100"
TRANSFER_RECEIVABLE = "1105"
CREDIT_LOSS_ALLOWANCE = "1109"
MARKETPLACE_RECEIVABLE = "1110"
SUPPLIER_REFUND_RECEIVABLE = "1120"
INVENTORY = "1200"
SUPPLIER_PREPAYMENTS = "1210"
GOODS_IN_TRANSIT = "1250"
PREPAID_TAX = "1400"
INTERCOMPANY_RECEIVABLE = "1900"
CUSTOMER_ADVANCES = "2200"  # contract liabilities (V3.1); 1300 is retired
SUPPLIER_PAYABLES = "2000"
CARRIER_PAYABLES = "2010"
ADS_PAYABLES = "2020"
REFUNDS_PAYABLE = "2100"
TAXES_PAYABLE = "2300"
INTERCOMPANY_PAYABLE = "2900"
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
VARIABLE_SERVICE = "5600"
CREDIT_LOSSES = "5700"
ADVERTISING = "6000"
OPEX = "6900"
