"""Payment service fixture that forwards operations to a gateway.

The separate class provides a cross-file service boundary for analysis tests.
"""

from payment.payment_gateway import PaymentGateway


class PaymentService:
    def __init__(self):
        self.gateway = PaymentGateway()

    def charge(self, order_id, amount):
        txn = self.gateway.authorize(order_id, amount)
        return self.gateway.capture(txn)

    def refund(self, txn_id):
        return self.gateway.reverse(txn_id)

    def status(self, txn_id):
        return self.gateway.lookup(txn_id)
