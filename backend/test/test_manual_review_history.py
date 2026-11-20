"""人工审核历史列表查询的回归测试。"""

import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock

from sqlalchemy.dialects.mysql import dialect as mysql_dialect

from app.modules.audit.field.crud import AuditFieldCRUD


class ManualReviewHistoryTests(unittest.TestCase):
    def test_page_reads_latest_rows_then_fetches_details_in_page_order(self):
        db = MagicMock()
        page_result = MagicMock()
        page_result.mappings.return_value.all.return_value = [
            {"confirmation_id": 20, "task_id": 2},
            {"confirmation_id": 10, "task_id": 1},
        ]
        detail_result = MagicMock()
        confirmation_10 = SimpleNamespace(id=10)
        confirmation_20 = SimpleNamespace(id=20)
        task_1 = SimpleNamespace(id=1)
        task_2 = SimpleNamespace(id=2)
        detail_result.all.return_value = [
            (confirmation_10, task_1),
            (confirmation_20, task_2),
        ]
        count_result = MagicMock()
        count_result.all.return_value = [(1, 3), (2, 1)]
        db.execute.side_effect = [page_result, detail_result, count_result]
        db.scalar.return_value = 2

        rows, total = AuditFieldCRUD(db).confirmation_history_page(page=1, page_size=20)

        self.assertEqual(total, 2)
        self.assertEqual(rows, [
            (confirmation_20, task_2, 1),
            (confirmation_10, task_1, 3),
        ])
        page_sql = str(db.execute.call_args_list[0].args[0].compile(dialect=mysql_dialect()))
        self.assertIn("WHERE NOT (EXISTS", page_sql)
        self.assertIn("ORDER BY field_review_confirmation.id DESC", page_sql)

    def test_empty_page_does_not_fetch_wide_detail_rows(self):
        db = MagicMock()
        page_result = MagicMock()
        page_result.mappings.return_value.all.return_value = []
        db.execute.return_value = page_result
        db.scalar.return_value = 4

        rows, total = AuditFieldCRUD(db).confirmation_history_page(page=2, page_size=20)

        self.assertEqual(rows, [])
        self.assertEqual(total, 4)
        db.execute.assert_called_once()


if __name__ == "__main__":
    unittest.main()
