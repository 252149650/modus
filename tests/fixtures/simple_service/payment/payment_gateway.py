"""Deterministic payment gateway fixture.

Fixed responses expose adapter methods to analysis without performing I/O.
"""


class PaymentGateway:
    def authorize(self, order_id, amount):
        return {"order": order_id, "amount": amount, "state": "authorized"}

    def capture(self, txn):
        txn["state"] = "captured"
        return txn

    def reverse(self, txn_id):
        return {"txn": txn_id, "state": "reversed"}

    def lookup(self, txn_id):
        return {"txn": txn_id, "state": "unknown"}
