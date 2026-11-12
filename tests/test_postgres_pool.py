"""PostgreSQL wrapper connection lifecycle tests."""

import unittest

from psycopg.pq import TransactionStatus

import db


class FakePool:
    def __init__(self):
        self.returned = []

    def putconn(self, connection):
        self.returned.append(connection)


class FakeConnection:
    def __init__(self, transaction_status):
        self.info = type("Info", (), {"transaction_status": transaction_status})()
        self.rollback_calls = 0

    def rollback(self):
        self.rollback_calls += 1
        self.info.transaction_status = TransactionStatus.IDLE


class PostgresPoolLifecycleTests(unittest.TestCase):
    def test_close_rolls_back_and_returns_connection_once(self):
        pool = FakePool()
        connection = FakeConnection(TransactionStatus.INTRANS)
        wrapper = db._PostgresConnection(connection, pool)

        wrapper.close()
        wrapper.close()

        self.assertEqual(connection.rollback_calls, 1)
        self.assertEqual(pool.returned, [connection])

    def test_close_returns_idle_connection_without_rollback(self):
        pool = FakePool()
        connection = FakeConnection(TransactionStatus.IDLE)

        db._PostgresConnection(connection, pool).close()

        self.assertEqual(connection.rollback_calls, 0)
        self.assertEqual(pool.returned, [connection])


if __name__ == "__main__":
    unittest.main()
