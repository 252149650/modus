"""Order service fixture with repository and payment dependencies.

Even an unused lookup is intentional evidence for call-graph extraction;
changing the fixture's calls changes the behavior being tested.
"""

from order.order_repo import OrderRepository


class OrderService:
    def __init__(self):
        self.repo = OrderRepository()

    def create_order(self, customer_id, items):
        order = self.repo.save(customer_id, items)
        self.notify(order)
        return order

    def get_order(self, order_id):
        return self.repo.find(order_id)

    def cancel_order(self, order_id):
        self.repo.find(order_id)
        return self.repo.delete(order_id)

    def notify(self, order):
        return f"order {order} created"
