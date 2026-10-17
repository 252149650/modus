"""In-memory order persistence for cross-file analysis fixtures.

Keep operations explicit: the fixture exercises service-to-repository call
binding without introducing a database dependency.
"""


class OrderRepository:
    def __init__(self):
        self._store = {}

    def save(self, customer_id, items):
        order_id = len(self._store) + 1
        self._store[order_id] = {"customer": customer_id, "items": items}
        return order_id

    def find(self, order_id):
        return self._store.get(order_id)

    def delete(self, order_id):
        return self._store.pop(order_id, None)

    def all_orders(self):
        return list(self._store.values())
