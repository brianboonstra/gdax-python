#
# cbpro/order_book.py
# David Caseria
#
# Live order book updated from the Coinbase Websocket Feed

from sortedcontainers import SortedDict
from decimal import Decimal
import pickle
import logging

from cbpro.public_client import PublicClient
from cbpro.websocket_client import WebsocketClient

logger = logging.getLogger(__name__)


def is_bid_side(m):
    if hasattr(m, 'strip'):
        return m[0] == "b"
    else:
        return is_bid_side(m['side'])


class OrderBook(WebsocketClient):
    def __init__(self, product_id='BTC-USD', log_to=None):
        super(OrderBook, self).__init__(products=product_id)
        self._asks = SortedDict()
        self._bids = SortedDict()
        self._client = PublicClient()
        self._sequence = -1
        self._log_to = log_to
        if self._log_to:
            assert hasattr(self._log_to, 'write')
        self._current_ticker = None

    @property
    def product_id(self):
        ''' Currently OrderBook only supports a single product even though it is stored as a list of products. '''
        return self.products[0]

    def on_open(self):
        self._sequence = -1
        print("-- Subscribed to OrderBook! --\n")

    def on_close(self):
        print("\n-- OrderBook Socket Closed! --")

    def reset_book(self):
        self._asks = SortedDict()
        self._bids = SortedDict()
        res = self._client.get_product_order_book(product_id=self.product_id, level=3)
        for bid in res['bids']:
            self.add({
                'id': bid[2],
                'side': 'buy',
                'price': Decimal(bid[0]),
                'size': Decimal(bid[1])
            })
        for ask in res['asks']:
            self.add({
                'id': ask[2],
                'side': 'sell',
                'price': Decimal(ask[0]),
                'size': Decimal(ask[1])
            })
        self._sequence = res['sequence']

    def on_message(self, message):
        if self._log_to:
            pickle.dump(message, self._log_to)

        sequence = message.get('sequence', -1)
        if self._sequence == -1:
            self.reset_book()
            return 'Reset book due to negative sequence'
        if sequence <= self._sequence:
            # ignore older messages (e.g. before order book initialization from getProductOrderBook)
            return 'Current book sequence beyond this order'
        elif sequence > self._sequence + 1:
            self.on_sequence_gap(self._sequence, sequence)
            return 'Sequence gap to this order'

        msg_type = message['type']
        book_change = 'Untreated message type'
        if msg_type == 'open':
            book_change = self.add(message)
        elif msg_type == 'done' and 'price' in message:
            book_change = self.remove(message)
        elif msg_type == 'done' and message.get('reason') == 'filled':
            book_change = self.remove(message)
        elif msg_type == 'match':
            book_change = self.match(message)
        elif msg_type == 'change':
            book_change = self.change(message)
        elif msg_type == 'received':
            book_change = None
        else:
            logger.info("Untreated message type %s: %s", msg_type, message)

        self._current_ticker = message.get('product_id')
        self._sequence = sequence

        return book_change

    def on_sequence_gap(self, gap_start, gap_end):
        self.reset_book()
        print('Error: messages missing ({} - {}). Re-initializing  book at sequence.'.format(
            gap_start, gap_end, self._sequence))

    def add(self, order):
        order = {
            'id': order.get('order_id') or order['id'],
            'side': order['side'],
            'price': Decimal(order['price']),
            'size': Decimal(order.get('size') or order['remaining_size'])
        }
        if order['side'] == 'buy':
            bids = self.get_bids(order['price'])
            if bids is None:
                bids = [order]
            else:
                bids.append(order)
            self.set_bids(order['price'], bids)
        else:
            asks = self.get_asks(order['price'])
            if asks is None:
                asks = [order]
            else:
                asks.append(order)
            self.set_asks(order['price'], asks)

        return order['price'], order['size']

    def remove(self, order):
        found_order = False
        removal_id = order.get('order_id')
        price = Decimal(order.get('price', -1))
        size = Decimal(order.get('size', 0))
        if order['side'] == 'buy':
            fbids = self.get_bids(price)
            if fbids is not None:
                bids = []
                for o in fbids:
                    if o['id'] != removal_id:
                        bids.append(o)
                    else:
                        osize = o['size']
                        if osize is not None and size == 0:
                            size = osize
                        if price == -1:
                            price = o['price']
                        found_order = True
                if len(bids) > 0:
                    self.set_bids(price, bids)
                else:
                    self.remove_bids(price)
        else:
            fasks = self.get_asks(price)
            if fasks is not None:
                asks = []
                for o in fasks:
                    if o['id'] != removal_id:
                        asks.append(o)
                    else:
                        osize = o['size']
                        if osize is not None and size == 0:
                            size = osize
                        if price == -1:
                            price = o['price']
                        found_order = True
                if len(asks) > 0:
                    self.set_asks(price, asks)
                else:
                    self.remove_asks(price)
        if not found_order:
            logger.debug("Failed to find order %s for removal", removal_id)
            size = 0
        if size is None:
            size = 0
        if price == -1:
            price = None
        return price, -size

    def match(self, order):
        size = Decimal(order['size'])
        price = Decimal(order['price'])

        if order['side'] == 'buy':
            bids = self.get_bids(price)
            if not bids:
                return 'Failed to find bids at this price for match'
            assert bids[0]['id'] == order['maker_order_id']
            if bids[0]['size'] == size:
                self.set_bids(price, bids[1:])
            else:
                bids[0]['size'] -= size
                self.set_bids(price, bids)
        else:
            asks = self.get_asks(price)
            if not asks:
                return 'Failed to find asks at this price for match'
            assert asks[0]['id'] == order['maker_order_id']
            if asks[0]['size'] == size:
                self.set_asks(price, asks[1:])
            else:
                asks[0]['size'] -= size
                self.set_asks(price, asks)

        return price, -size

    def change(self, order):
        try:
            new_size = Decimal(order['new_size'])
        except KeyError:
            return 'Failed to find new size at this price for change'

        try:
            price = Decimal(order['price'])
        except KeyError:
            return 'Failed to find new price for change'

        if order['side'] == 'buy':
            bids = self.get_bids(price)
            if bids is None or not any(o['id'] == order['order_id'] for o in bids):
                return 'Failed to find bids at this price for change'
            index = [b['id'] for b in bids].index(order['order_id'])
            old_size = bids[index]['size']
            bids[index]['size'] = new_size
            self.set_bids(price, bids)
        else:
            asks = self.get_asks(price)
            if asks is None or not any(o['id'] == order['order_id'] for o in asks):
                return 'Failed to find asks at this price for change'
            index = [a['id'] for a in asks].index(order['order_id'])
            old_size = asks[index]['size']
            asks[index]['size'] = new_size
            self.set_asks(price, asks)

        tree = self._bids if is_bid_side(order['side']) else self._asks
        node = tree.get(price)

        if node is None or not any(o['id'] == order['order_id'] for o in node):
            return 'Failed to find node at this price for change'

        size_change = new_size - old_size
        return price, size_change

    def get_current_ticker(self):
        return self._current_ticker

    def get_current_book(self):
        result = {
            'sequence': self._sequence,
            'asks': [],
            'bids': [],
        }
        for ask in self._asks:
            try:
                # There can be a race condition here, where a price point is removed
                # between these two ops
                this_ask = self._asks[ask]
            except KeyError:
                continue
            for order in this_ask:
                # noinspection PyTypeChecker
                result['asks'].append([order['price'], order['size'], order['id']])
        for bid in self._bids:
            try:
                # There can be a race condition here, where a price point is removed
                # between these two ops
                this_bid = self._bids[bid]
            except KeyError:
                continue

            for order in this_bid:
                # noinspection PyTypeChecker
                result['bids'].append([order['price'], order['size'], order['id']])
        return result

    def get_ask(self):
        return self._asks.peekitem(0)[0]

    def get_asks(self, price):
        return self._asks.get(price)

    def remove_asks(self, price):
        logger.debug("Deleting asks at %s", price)
        del self._asks[price]

    def set_asks(self, price, asks):
        self._asks[price] = asks

    def get_bid(self):
        return self._bids.peekitem(-1)[0]

    def get_bids(self, price):
        return self._bids.get(price)

    def remove_bids(self, price):
        logger.debug("Deleting bids at %s", price)
        del self._bids[price]

    def set_bids(self, price, bids):
        self._bids[price] = bids


if __name__ == '__main__':
    import sys
    import time
    import datetime as dt


    class OrderBookConsole(OrderBook):
        ''' Logs real-time changes to the bid-ask spread to the console '''

        def __init__(self, product_id=None):
            super(OrderBookConsole, self).__init__(product_id=product_id)

            # latest values of bid-ask spread
            self._bid = None
            self._ask = None
            self._bid_depth = None
            self._ask_depth = None

        def on_message(self, message):
            super(OrderBookConsole, self).on_message(message)

            # Calculate newest bid-ask spread
            bid = self.get_bid()
            bids = self.get_bids(bid)
            bid_depth = sum([b['size'] for b in bids])
            ask = self.get_ask()
            asks = self.get_asks(ask)
            ask_depth = sum([a['size'] for a in asks])

            if self._bid == bid and self._ask == ask and self._bid_depth == bid_depth and self._ask_depth == ask_depth:
                # If there are no changes to the bid-ask spread since the last update, no need to print
                pass
            else:
                # If there are differences, update the cache
                self._bid = bid
                self._ask = ask
                self._bid_depth = bid_depth
                self._ask_depth = ask_depth
                print('{} {} bid: {:.3f} @ {:.2f}\task: {:.3f} @ {:.2f}'.format(
                    dt.datetime.now(), self.product_id, bid_depth, bid, ask_depth, ask))

    order_book = OrderBookConsole()
    order_book.start()
    try:
        while True:
            time.sleep(10)
    except KeyboardInterrupt:
        order_book.close()

    if order_book.error:
        sys.exit(1)
    else:
        sys.exit(0)
