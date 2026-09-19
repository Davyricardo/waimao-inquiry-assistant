"""路由包：将 web.py 按功能域拆分为独立的 APIRouter。

各子模块：
- dashboard  : GET /、/api/stats、/api/sync
- messages   : GET/POST /messages、/api/messages
- contacts   : GET/POST /contacts、/api/contacts
- orders     : GET/POST /orders、/api/orders
- freight    : GET/POST /freight、/api/freight
- products   : GET/POST /products、/api/products
- social     : GET/POST /social、/api/social
- company    : GET/POST /company
- settings   : GET/POST /settings、/rules、/templates、/review
"""
from . import company, contacts, dashboard, freight, logs, messages, orders, products, settings, social, vouchers

__all__ = [
    "dashboard",
    "messages",
    "contacts",
    "orders",
    "vouchers",
    "freight",
    "products",
    "social",
    "company",
    "logs",
    "settings",
]
