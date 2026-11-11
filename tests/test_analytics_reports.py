import os
import re
import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from urllib.parse import urlencode

from werkzeug.security import generate_password_hash

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import db as dbh  # noqa: E402

_boot_dir = tempfile.mkdtemp()
dbh.DB_PATH = os.path.join(_boot_dir, "boot.db")
import app as A  # noqa: E402


class AnalyticsReportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._old_path = dbh.DB_PATH
        cls._old_ready = A._db_ready
        cls._temp_dir = tempfile.mkdtemp()
        dbh.DB_PATH = os.path.join(cls._temp_dir, "analytics.db")
        dbh.init_db()
        A._db_ready = True
        conn = dbh.get_db()
        cls.admin_id = conn.execute(
            "INSERT INTO users (full_name,email,password_hash,role) VALUES (?,?,?,'admin')",
            ("Analytics Admin", "analytics-admin@example.test", generate_password_hash("Admin@123")),
        ).lastrowid
        cls.bpso_id = conn.execute(
            "INSERT INTO users (full_name,email,password_hash,role,office) VALUES (?,?,?,'staff','bpso')",
            ("Analytics BPSO", "analytics-bpso@example.test", generate_password_hash("Staff@123")),
        ).lastrowid
        cls.secretary_id = conn.execute(
            "INSERT INTO users (full_name,email,password_hash,role,office) VALUES (?,?,?,'staff','secretary')",
            ("Analytics Secretary", "analytics-secretary@example.test", generate_password_hash("Staff@123")),
        ).lastrowid
        cls.unassigned_id = conn.execute(
            "INSERT INTO users (full_name,email,password_hash,role) VALUES (?,?,?,'staff')",
            ("Unassigned Staff", "analytics-unassigned@example.test", generate_password_hash("Staff@123")),
        ).lastrowid
        resident_id = conn.execute(
            "INSERT INTO users (full_name,email,password_hash,role) VALUES (?,?,?,'resident')",
            ("Analytics Resident", "analytics-resident@example.test", generate_password_hash("Resident@123")),
        ).lastrowid
        cls.resident_id = resident_id
        cls.second_admin_id = conn.execute(
            "INSERT INTO users (full_name,email,password_hash,role) VALUES (?,?,?,'admin')",
            ("Second Analytics Admin", "analytics-admin-two@example.test", generate_password_hash("Admin@123")),
        ).lastrowid

        today = date.today()
        current_date = today.replace(day=10) if today.day >= 10 else today
        prior_month_end = (today.replace(day=1) - timedelta(days=1))
        prior_month_date = prior_month_end.replace(day=10) if prior_month_end.day >= 10 else prior_month_end
        conn.executemany(
            "INSERT INTO complaints (reference_no,resident_id,category,description,status,is_verified,created_at) "
            "VALUES (?,?,?,?,?,?,?)",
            [
                ("ANA-C-1", resident_id, "Noise Complaint", "Noise report", "Pending", 0,
                 f"{current_date.isoformat()} 12:00:00"),
                ("ANA-C-2", resident_id, "Sanitation", "Waste report", "Resolved", 1,
                 f"{prior_month_date.isoformat()} 12:00:00"),
            ],
        )
        conn.executemany(
            "INSERT INTO requests (reference_no,resident_id,request_type,status,created_at) "
            "VALUES (?,?,?,?,?)",
            [
                ("ANA-R-1", resident_id, "Barangay Clearance", "Approved",
                 f"{current_date.isoformat()} 12:00:00"),
                ("ANA-R-2", resident_id, "Certificate of Residency", "Submitted",
                 f"{prior_month_date.isoformat()} 12:00:00"),
            ],
        )
        conn.commit()
        conn.close()

    @classmethod
    def tearDownClass(cls):
        dbh.DB_PATH = cls._old_path
        A._db_ready = cls._old_ready

    def client_as(self, user_id, role):
        client = A.app.test_client()
        with client.session_transaction() as session:
            session["user_id"] = user_id
            session["role"] = role
        return client

    def add_complaint(self, reference, category, created_at, status="Pending", description="Private complaint details"):
        conn = dbh.get_db()
        conn.execute(
            "INSERT INTO complaints (reference_no,resident_id,category,description,status,created_at) "
            "VALUES (?,?,?,?,?,?)",
            (reference, self.resident_id, category, description, status, created_at),
        )
        conn.commit()
        conn.close()

    def add_request(self, reference, request_type, created_at, status="Submitted", purpose="Private purpose"):
        conn = dbh.get_db()
        conn.execute(
            "INSERT INTO requests (reference_no,resident_id,request_type,purpose,status,created_at) "
            "VALUES (?,?,?,?,?,?)",
            (reference, self.resident_id, request_type, purpose, status, created_at),
        )
        conn.commit()
        conn.close()

    def analytics_csrf_token(self, client):
        response = client.get("/admin/reports/complaints")
        self.assertEqual(response.status_code, 200)
        match = re.search(rb'data-csrf-token="([^"]+)"', response.data)
        self.assertIsNotNone(match)
        return match.group(1).decode("ascii")

    def test_complaint_analytics_uses_philippine_day_and_includes_all_statuses(self):
        self.add_complaint(
            "C-UTC-BEFORE", "Before PH day", "2024-12-31 15:59:59"
        )
        self.add_complaint(
            "C-UTC-START", "<svg onload=alert(1)>", "2024-12-31 16:00:00", "Rejected"
        )
        self.add_complaint(
            "C-UTC-END", "Legacy Category", "2025-01-01 15:59:59", "Closed"
        )
        self.add_complaint(
            "C-UTC-NEXT", "After PH day", "2025-01-01 16:00:00"
        )

        response = self.client_as(self.admin_id, "admin").get(
            "/admin/complaint-analytics/data?period=daily&daily_date=2025-01-01"
        )
        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertEqual(data["total"], 2)
        self.assertEqual(
            [(row["label"], row["count"]) for row in data["items"]],
            [("<svg onload=alert(1)>", 1), ("Legacy Category", 1)],
        )
        self.assertEqual(data["leaders"], ["<svg onload=alert(1)>", "Legacy Category"])
        self.assertNotIn(b"Analytics Resident", response.data)
        self.assertNotIn(b"Private complaint details", response.data)

        html = self.client_as(self.admin_id, "admin").get(
            "/reports?period=daily&daily_date=2025-01-01"
        ).get_data(as_text=True)
        self.assertIn("Complaint Analytics", html)
        self.assertIn("Document Request Analytics", html)
        complaint_html = self.client_as(self.admin_id, "admin").get(
            "/admin/reports/complaints?period=daily&daily_date=2025-01-01"
        ).get_data(as_text=True)
        self.assertIn("&lt;svg onload=alert(1)&gt;", complaint_html)
        self.assertNotIn("<svg onload=alert(1)>", complaint_html)
        complaint_page = self.client_as(self.admin_id, "admin").get(
            "/admin/reports/complaints?period=daily&daily_date=2025-01-01"
        ).get_data(as_text=True)
        self.assertIn("Complaint Analytics", complaint_page)
        self.assertIn("data-analytics-total", complaint_page)
        self.assertIn("data-analytics-chart", complaint_page)
        self.assertIn("Save Colors", complaint_page)

    def test_complaint_analytics_month_year_and_custom_filters(self):
        self.add_complaint("C-MONTH-START", "Monthly Start", "2025-01-31 16:00:00")
        self.add_complaint("C-MONTH-END", "Monthly End", "2025-02-28 15:59:59")
        self.add_complaint("C-MONTH-NEXT", "Next Month", "2025-02-28 16:00:00")
        client = self.client_as(self.admin_id, "admin")

        monthly = client.get(
            "/admin/complaint-analytics/data?period=monthly&month=2025-02"
        ).get_json()
        self.assertEqual(monthly["total"], 2)
        self.assertEqual({row["label"] for row in monthly["items"]}, {"Monthly Start", "Monthly End"})

        yearly = client.get(
            "/admin/complaint-analytics/data?period=yearly&year=2025"
        ).get_json()
        self.assertEqual(yearly["total"], 3)

        custom = client.get(
            "/admin/complaint-analytics/data?period=custom"
            "&analytics_start=2025-02-01&analytics_end=2025-02-28"
        ).get_json()
        self.assertEqual(custom["total"], 2)

    def test_document_request_analytics_counts_by_type_and_philippine_boundaries(self):
        self.add_request(
            "R-UTC-BEFORE", "Before PH month", "2024-01-31 15:59:59",
            purpose="Should not group by purpose",
        )
        self.add_request(
            "R-UTC-START", "Legacy Document", "2024-02-28 16:00:00",
            "Rejected", purpose="Same purpose",
        )
        self.add_request(
            "R-UTC-END", "Legacy Document", "2024-02-29 15:59:59",
            "Approved", purpose="Same purpose",
        )
        self.add_request(
            "R-UTC-NEXT", "After PH day", "2024-02-29 16:00:00",
            purpose="Same purpose",
        )
        client = self.client_as(self.admin_id, "admin")
        response = client.get(
            "/admin/reports/document-requests/data?period=monthly&month=2024-02"
        )
        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertEqual(data["total"], 2)
        self.assertEqual(data["items"], [{
            "label": "Legacy Document",
            "count": 2,
            "color": data["items"][0]["color"],
            "x": data["items"][0]["x"],
            "bar_width": 46,
            "bar_height": 210,
            "bar_y": 50,
            "label_x": 85,
            "label_y": 279,
            "value_y": 42,
        }])
        self.assertEqual(data["leaders"], ["Legacy Document"])
        self.assertNotIn("Same purpose", response.get_data(as_text=True))
        self.assertNotIn("Analytics Resident", response.get_data(as_text=True))

        one_day = client.get(
            "/admin/reports/document-requests/data?period=custom"
            "&analytics_start=2024-02-29&analytics_end=2024-02-29"
        ).get_json()
        self.assertEqual(one_day["total"], 2)

        page = client.get("/admin/reports/document-requests?period=monthly&month=2024-02")
        self.assertEqual(page.status_code, 200)
        html = page.get_data(as_text=True)
        self.assertIn("Total Document Requests", html)
        self.assertIn("Most Requested Document Type", html)
        self.assertIn("Back to Reports &amp; Analytics", html)
        self.assertIn("Legacy Document", html)

    def test_document_request_analytics_yearly_ties_and_empty_results(self):
        self.add_request("R-TIE-1", "Certificate A", "2028-01-01 00:00:00", "Rejected")
        self.add_request("R-TIE-2", "Certificate B", "2028-02-01 00:00:00", "Completed")
        client = self.client_as(self.admin_id, "admin")
        yearly = client.get(
            "/admin/reports/document-requests/data?period=yearly&year=2028"
        ).get_json()
        self.assertEqual(yearly["total"], sum(row["count"] for row in yearly["items"]))
        self.assertEqual(yearly["leaders"], ["Certificate A", "Certificate B"])
        empty = client.get(
            "/admin/reports/document-requests/data?period=yearly&year=2099"
        ).get_json()
        self.assertEqual(empty["total"], 0)
        self.assertEqual(empty["items"], [])
        self.assertEqual(empty["leaders"], [])

    def test_purpose_analytics_groups_real_request_purposes_by_office_and_ph_time(self):
        self.add_request(
            "PURPOSE-INDIGENCY-MED", "Certificate of Indigency",
            "2025-03-31 16:00:00", "Rejected", "Medical Assistance",
        )
        self.add_request(
            "PURPOSE-INDIGENCY-BLANK", "Certificate of Indigency",
            "2025-04-10 12:00:00", "Approved", "  ",
        )
        self.add_request(
            "PURPOSE-INDIGENCY-MED-2", "Certificate of Indigency",
            "2025-04-20 12:00:00", "Under Review", "Medical Assistance",
        )
        self.add_request(
            "PURPOSE-RESIDENCY-SCHOLARSHIP", "Certificate of Residency",
            "2025-04-30 15:59:59", "Completed", "Scholarship ID",
        )
        self.add_request(
            "PURPOSE-CLEARANCE-EMPLOYMENT", "Barangay Clearance",
            "2025-04-15 00:00:00", "Submitted", "Local Employment",
        )
        self.add_request(
            "PURPOSE-CLEARANCE-NEXT-MONTH", "Barangay Clearance",
            "2025-04-30 16:00:00", "Submitted", "Custom employment reason",
        )
        admin = self.client_as(self.admin_id, "admin")

        secretary = admin.get(
            "/reports/request-purposes/data?period=monthly&month=2025-04"
            "&office=secretary"
        )
        self.assertEqual(secretary.status_code, 200)
        data = secretary.get_json()
        self.assertEqual(data["total"], 4)
        self.assertEqual(
            [(row["purpose"], row["count"]) for row in data["items"]],
            [("Medical Assistance", 2), ("Scholarship ID", 1), ("Unspecified", 1)],
        )
        self.assertEqual(
            [(row["label"], row["count"]) for row in data["items"]],
            [("Medical Assistance", 2), ("Scholarship ID", 1), ("Unspecified", 1)],
        )
        self.assertEqual(data["most_common_purpose"], "Medical Assistance")
        self.assertEqual(data["most_common_purpose_count"], 2)

        indigency = admin.get(
            "/reports/request-purposes/data?period=monthly&month=2025-04"
            "&office=secretary&document_type=Certificate+of+Indigency"
        ).get_json()
        self.assertEqual(indigency["total"], 3)
        self.assertEqual(
            [(row["purpose"], row["count"]) for row in indigency["items"]],
            [("Medical Assistance", 2), ("Unspecified", 1)],
        )
        self.assertEqual(
            [(row["label"], row["count"]) for row in indigency["items"]],
            [("Medical Assistance", 2), ("Unspecified", 1)],
        )

        treasurer = admin.get(
            "/reports/request-purposes/data?period=monthly&month=2025-04"
            "&office=treasurer"
        ).get_json()
        self.assertEqual(treasurer["total"], 1)
        self.assertEqual(treasurer["items"][0]["label"], "Local Employment")
        no_document_office = admin.get(
            "/reports/request-purposes/data?period=monthly&month=2025-04"
            "&office=bpso"
        ).get_json()
        self.assertEqual(no_document_office["total"], 0)
        self.assertEqual(
            admin.get("/reports/request-purposes/data?office=unknown").status_code,
            400,
        )

    def test_staff_purpose_analytics_is_limited_to_assigned_office(self):
        self.add_request(
            "PURPOSE-STAFF-SECRETARY", "Certificate of Indigency",
            "2055-06-01 00:00:00", "Rejected", "Custom scholarship reason",
        )
        self.add_request(
            "PURPOSE-STAFF-TREASURER", "Barangay Clearance",
            "2055-06-02 00:00:00", "Approved", "Local Employment",
        )
        secretary = self.client_as(self.secretary_id, "staff")
        result = secretary.get(
            "/reports/request-purposes/data?period=yearly&year=2055"
        )
        self.assertEqual(result.status_code, 200)
        data = result.get_json()
        self.assertEqual(data["total"], 1)
        self.assertEqual(data["items"][0]["purpose"], "Custom scholarship reason")
        self.assertEqual(data["items"][0]["label"], "Custom scholarship reason")

        other_office_type = secretary.get(
            "/reports/request-purposes/data?period=yearly&year=2055"
            "&document_type=Barangay+Clearance&office=all"
        ).get_json()
        self.assertEqual(other_office_type["total"], 0)

        bpso = self.client_as(self.bpso_id, "staff")
        self.assertEqual(
            bpso.get("/reports/request-purposes/data?period=yearly&year=2055").status_code,
            403,
        )
        self.assertEqual(
            bpso.get("/reports").status_code,
            200,
        )
        staff_page = secretary.get("/reports").get_data(as_text=True)
        self.assertIn("Requests by Purpose", staff_page)
        self.assertNotIn("Purpose / Reason (x-axis)", staff_page)
        self.assertNotIn("All statuses · Philippine time", staff_page)
        self.assertIn("Certificate of Indigency", staff_page)
        bpso_page = bpso.get("/reports").get_data(as_text=True)
        self.assertIn("Most Common Complaints", bpso_page)
        self.assertNotIn("Requests by Purpose", bpso_page)

        resident = self.client_as(self.resident_id, "resident")
        self.assertEqual(
            resident.get("/reports/request-purposes/data").status_code, 403
        )

    def test_purpose_analytics_defaults_to_current_philippine_month(self):
        client = self.client_as(self.admin_id, "admin")
        page = client.get("/admin/reports/document-requests")
        self.assertEqual(page.status_code, 200)
        current_month = datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m")
        html = page.get_data(as_text=True)
        self.assertIn("Requests by Purpose", html)
        self.assertNotIn("Number of Requests (y-axis)", html)
        month_input = re.search(
            r'<input\b[^>]*id="purpose-analytics-month"[^>]*>', html, re.DOTALL
        )
        self.assertIsNotNone(month_input)
        self.assertIn(f'value="{current_month}"', month_input.group(0))
        self.assertIn('<option value="all" selected>All offices</option>', html)

    def test_print_reports_include_branded_filtered_print_layout_without_resident_details(self):
        admin = self.client_as(self.admin_id, "admin")
        for path, title in (
            ("/admin/reports/complaints?period=yearly&year=2025", "Complaint Analytics"),
            ("/admin/reports/document-requests?period=yearly&year=2025", "Document Request Analytics"),
        ):
            response = admin.get(path)
            self.assertEqual(response.status_code, 200)
            html = response.get_data(as_text=True)
            self.assertIn("Print Report", html)
            self.assertIn("BarangayConnect · Service Analytics", html)
            self.assertIn(title, html)
            self.assertIn('data-print-filter="module-period">2025', html)
            self.assertNotIn("Analytics Resident", html)
            self.assertNotIn("Private purpose", html)
            if "document-requests" in path:
                self.assertIn('data-print-filter="purpose-document-type">All documents', html)
                self.assertIn('data-print-filter="purpose-office">All offices', html)
                self.assertNotIn("Purpose period", html)

        staff = self.client_as(self.secretary_id, "staff")
        response = staff.get(
            "/reports?range=custom&start_date=2025-01-01&end_date=2025-01-31"
        )
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn("Print Report", html)
        self.assertIn("BarangayConnect · Service Analytics", html)
        self.assertIn("Document requests by type", html)
        self.assertIn("Requests by purpose", html)
        self.assertIn('<dt>Document type</dt>', html)
        self.assertIn('<dt>Office</dt>', html)
        self.assertNotIn("Purpose document type", html)
        self.assertNotIn("Purpose period", html)
        self.assertIn('data-print-filter="staff-period">Custom Range', html)
        self.assertNotIn("Analytics Resident", html)

        with open(os.path.join(ROOT, "static", "css", "style.css"), encoding="utf-8") as css_file:
            print_css = css_file.read()
        self.assertIn("@media print", print_css)
        self.assertIn("size: A4 portrait", print_css)
        self.assertIn('counter(page)', print_css)
        self.assertIn("print-color-adjust: exact", print_css)
        self.assertIn("#admin-report-analytics {", print_css)
        self.assertRegex(
            print_css,
            r"\.report-analytics-chart-card\s*\{[^}]*\bbreak-inside:\s*auto\s*;",
        )
        self.assertRegex(
            print_css,
            r"\.report-analytics-chart\s*\{[^}]*\bbreak-inside:\s*avoid\s*;",
        )
        self.assertRegex(
            print_css,
            r"\.analytics-line-chart\s+svg\s*\{"
            r"[^}]*width: 100% !important;[^}]*max-width: 100% !important;"
            r"[^}]*height: auto !important;",
        )
        self.assertRegex(
            print_css,
            r"\.report-analytics-chart\s+svg\s*\{"
            r"[^}]*width: 100% !important;[^}]*max-width: 100% !important;"
            r"[^}]*height: auto !important;",
        )

    def test_document_request_colors_are_separate_from_complaint_colors(self):
        self.add_request("R-COLOR", "Noise Complaint", "2050-01-01 00:00:00")
        admin = self.client_as(self.admin_id, "admin")
        token = self.analytics_csrf_token(admin)
        saved = admin.post(
            "/admin/reports/document-requests/colors",
            json={
                "mode": "save",
                "csrf_token": token,
                "colors": {"Noise Complaint": "#ABC123"},
            },
        )
        self.assertEqual(saved.status_code, 200)
        complaint_color = admin.get(
            "/admin/reports/complaints/data?period=daily&daily_date=2050-01-01"
        ).get_json()["items"][0]["color"]
        request_color = admin.get(
            "/admin/reports/document-requests/data?period=daily&daily_date=2050-01-01"
        ).get_json()["items"][0]["color"]
        self.assertNotEqual(complaint_color, request_color)
        self.assertEqual(request_color, "#ABC123")

        reset = admin.post(
            "/admin/reports/document-requests/colors",
            json={"mode": "reset", "csrf_token": token},
        )
        self.assertEqual(reset.status_code, 200)
        reset_color = admin.get(
            "/admin/reports/document-requests/data?period=daily&daily_date=2050-01-01"
        ).get_json()["items"][0]["color"]
        self.assertNotEqual(reset_color, request_color)

    def test_module_defaults_to_current_philippine_month(self):
        client = self.client_as(self.admin_id, "admin")
        page = client.get("/admin/reports/complaints")
        current_month = datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m")
        html = page.get_data(as_text=True)
        self.assertIn(f'value="{current_month}"', html)
        self.assertIn('<option value="monthly" selected>', html)

    def test_all_new_reports_routes_require_admin(self):
        staff = self.client_as(self.bpso_id, "staff")
        resident = self.client_as(self.resident_id, "resident")
        paths = (
            "/admin/reports/complaints",
            "/admin/reports/complaints/data",
            "/admin/reports/complaints/colors",
            "/admin/reports/document-requests",
            "/admin/reports/document-requests/data",
            "/admin/reports/document-requests/colors",
        )
        for client in (staff, resident):
            for path in paths:
                response = client.post(path, json={}) if path.endswith("/colors") else client.get(path)
                self.assertEqual(response.status_code, 403, path)
        anonymous = A.app.test_client()
        self.assertEqual(
            anonymous.get("/admin/reports/document-requests").status_code, 302
        )

    def test_complaint_analytics_invalid_and_empty_ranges(self):
        client = self.client_as(self.admin_id, "admin")
        invalid = client.get(
            "/admin/complaint-analytics/data?period=custom"
            "&analytics_start=2025-02-02&analytics_end=2025-02-01"
        )
        self.assertEqual(invalid.status_code, 400)
        self.assertIn("on or after", invalid.get_json()["error"])
        invalid_date = client.get(
            "/admin/complaint-analytics/data?period=daily&daily_date=not-a-date"
        )
        self.assertEqual(invalid_date.status_code, 400)
        self.assertIn("valid date", invalid_date.get_json()["error"])

        empty = client.get(
            "/admin/complaint-analytics/data?period=daily&daily_date=2099-01-01"
        )
        self.assertEqual(empty.status_code, 200)
        self.assertEqual(empty.get_json()["total"], 0)
        self.assertEqual(empty.get_json()["items"], [])

        page = client.get(
            "/admin/reports/complaints?period=custom&analytics_start=2025-02-02&analytics_end=2025-02-01"
        )
        self.assertEqual(page.status_code, 200)
        self.assertIn("end date must be on or after", page.get_data(as_text=True))
        self.assertIn("No complaints match this period.", page.get_data(as_text=True))

    def test_complaint_analytics_admin_rbac(self):
        staff = self.client_as(self.bpso_id, "staff")
        resident = self.client_as(self.resident_id, "resident")
        for client in (staff, resident):
            self.assertEqual(
                client.get("/admin/complaint-analytics/data?period=daily&daily_date=2025-01-01").status_code,
                403,
            )
            self.assertEqual(
                client.post(
                    "/admin/complaint-analytics/colors",
                    json={"mode": "reset", "csrf_token": "invalid"},
                ).status_code,
                403,
            )
        staff_page = staff.get("/reports").get_data(as_text=True)
        self.assertNotIn("Reports &amp; Analytics</h2>", staff_page)
        self.assertIn("Most Common Complaints", staff_page)

    def test_complaint_analytics_colors_are_validated_scoped_and_audited(self):
        self.add_complaint("C-COLOR", "Noise Complaint", "2050-01-01 00:00:00")
        admin = self.client_as(self.admin_id, "admin")
        second_admin = self.client_as(self.second_admin_id, "admin")
        token = self.analytics_csrf_token(admin)
        self.analytics_csrf_token(second_admin)

        missing_csrf = admin.post(
            "/admin/complaint-analytics/colors",
            json={"mode": "save", "colors": {"Noise Complaint": "#123456"}},
        )
        self.assertEqual(missing_csrf.status_code, 403)
        invalid_color = admin.post(
            "/admin/complaint-analytics/colors",
            json={
                "mode": "save",
                "csrf_token": token,
                "colors": {"Noise Complaint": "red"},
            },
        )
        self.assertEqual(invalid_color.status_code, 400)

        saved = admin.post(
            "/admin/complaint-analytics/colors",
            json={
                "mode": "save",
                "csrf_token": token,
                "colors": {"Noise Complaint": "#12aBcF"},
            },
        )
        self.assertEqual(saved.status_code, 200)
        first_color = admin.get(
            "/admin/complaint-analytics/data?period=daily&daily_date=2050-01-01"
        ).get_json()["items"][0]["color"]
        second_color = second_admin.get(
            "/admin/complaint-analytics/data?period=daily&daily_date=2050-01-01"
        ).get_json()["items"][0]["color"]
        self.assertEqual(first_color, "#12ABCF")
        self.assertNotEqual(second_color, first_color)

        conn = dbh.get_db()
        audit = conn.execute(
            "SELECT action, details FROM audit_logs WHERE actor_id=? "
            "AND action='Updated complaint analytics colors' ORDER BY id DESC LIMIT 1",
            (self.admin_id,),
        ).fetchone()
        self.assertEqual(audit["details"], "Saved 1 label color preferences.")
        self.assertNotIn("Analytics Resident", audit["details"])
        conn.close()

        reset = admin.post(
            "/admin/complaint-analytics/colors",
            json={"mode": "reset", "csrf_token": token},
        )
        self.assertEqual(reset.status_code, 200)
        reset_color = admin.get(
            "/admin/complaint-analytics/data?period=daily&daily_date=2050-01-01"
        ).get_json()["items"][0]["color"]
        self.assertNotEqual(reset_color, first_color)

    def test_admin_chart_preferences_are_added_by_repeatable_migration(self):
        original_path = dbh.DB_PATH
        migration_path = os.path.join(self._temp_dir, "migration.db")
        dbh.DB_PATH = migration_path
        try:
            dbh.init_db()
            conn = dbh.get_db()
            conn.execute("DROP TABLE admin_chart_preferences")
            conn.execute(
                "CREATE TABLE admin_chart_preferences ("
                "admin_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE, "
                "category TEXT NOT NULL, color TEXT NOT NULL, "
                "updated_at TEXT NOT NULL DEFAULT (datetime('now')), "
                "PRIMARY KEY (admin_id, category))"
            )
            admin_id = conn.execute(
                "INSERT INTO users (full_name,email,password_hash,role) "
                "VALUES ('Migration Admin','migration@example.test','hash','admin')"
            ).lastrowid
            conn.execute(
                "INSERT INTO admin_chart_preferences (admin_id, category, color) "
                "VALUES (?, 'Old Category', '#ABCDEF')",
                (admin_id,),
            )
            conn.commit()
            conn.close()
            dbh.migrate_db()
            dbh.migrate_db()
            conn = dbh.get_db()
            columns = {
                row["name"] for row in conn.execute(
                    "PRAGMA table_info(admin_chart_preferences)"
                ).fetchall()
            }
            self.assertEqual(
                columns, {"admin_id", "analytics_type", "category", "color", "updated_at"}
            )
            preference = conn.execute(
                "SELECT analytics_type, category, color FROM admin_chart_preferences"
            ).fetchone()
            self.assertEqual(tuple(preference), ("complaints", "Old Category", "#ABCDEF"))
        finally:
            dbh.DB_PATH = original_path

    def test_admin_analytics_filters_and_previous_period_counts(self):
        today = date.today()
        current_month = today.replace(day=1)
        next_month = (current_month.replace(day=28) + timedelta(days=4)).replace(day=1)
        query = urlencode({
            "range": "custom",
            "start_date": current_month.isoformat(),
            "end_date": (next_month - timedelta(days=1)).isoformat(),
        })
        client = self.client_as(self.bpso_id, "staff")
        response = client.get(f"/reports?{query}")
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn("Most Common Complaints", html)
        self.assertIn("Monthly Transaction Trends", html)
        self.assertIn("Noise Complaint", html)

        admin = self.client_as(self.admin_id, "admin")
        selection = admin.get("/admin/reports")
        self.assertEqual(selection.status_code, 200)
        selection_html = selection.get_data(as_text=True)
        self.assertIn("Complaint Analytics", selection_html)
        self.assertIn("Document Request Analytics", selection_html)
        self.assertIn('href="/admin/reports/complaints"', selection_html)
        self.assertIn('href="/admin/reports/document-requests"', selection_html)
        self.assertNotIn("Most Common Complaints", selection_html)

    def test_staff_analytics_are_scoped_to_assigned_office(self):
        bpso = self.client_as(self.bpso_id, "staff")
        bpso_page = bpso.get("/reports")
        self.assertEqual(bpso_page.status_code, 200)
        bpso_html = bpso_page.get_data(as_text=True)
        self.assertIn("Noise Complaint", bpso_html)
        self.assertNotIn("Barangay Clearance", bpso_html)
        self.assertNotIn("Most Requested Documents", bpso_html)

        secretary = self.client_as(self.secretary_id, "staff")
        secretary_page = secretary.get("/reports")
        self.assertEqual(secretary_page.status_code, 200)
        secretary_html = secretary_page.get_data(as_text=True)
        self.assertIn("Certificate of Residency", secretary_html)
        self.assertIn("Most Requested Documents", secretary_html)
        self.assertNotIn("Noise Complaint", secretary_html)
        self.assertNotIn("Most Common Complaints", secretary_html)

        unassigned = self.client_as(self.unassigned_id, "staff")
        self.assertEqual(unassigned.get("/reports").status_code, 403)
        self.assertEqual(unassigned.get("/reports/export?format=csv").status_code, 403)

    def test_filtered_exports_include_real_report_data(self):
        client = self.client_as(self.admin_id, "admin")
        csv_response = client.get("/reports/export?range=all&format=csv")
        self.assertEqual(csv_response.status_code, 200)
        self.assertIn(b"Noise Complaint", csv_response.data)
        self.assertIn(b"Barangay Clearance", csv_response.data)

        pdf_response = client.get("/reports/export?range=all&format=pdf")
        self.assertEqual(pdf_response.status_code, 200)
        self.assertEqual(pdf_response.mimetype, "application/pdf")
        self.assertTrue(pdf_response.data.startswith(b"%PDF"))

        xlsx_response = client.get("/reports/export?range=all&format=xlsx")
        self.assertEqual(xlsx_response.status_code, 200)
        self.assertTrue(xlsx_response.data.startswith(b"PK"))

        filtered_csv = client.get(
            "/reports/export?range=all&category=Noise+Complaint&format=csv"
        )
        self.assertIn(b"Noise Complaint", filtered_csv.data)
        self.assertNotIn(b"Sanitation", filtered_csv.data)

    def test_invalid_custom_range_is_reported_without_server_error(self):
        client = self.client_as(self.bpso_id, "staff")
        response = client.get("/reports?range=custom&start_date=2026-12-20&end_date=2026-12-01")
        self.assertEqual(response.status_code, 200)
        self.assertIn("end date must be on or after", response.get_data(as_text=True))

    def test_empty_date_range_shows_empty_state(self):
        client = self.client_as(self.bpso_id, "staff")
        future = date.today() + timedelta(days=3650)
        response = client.get(
            f"/reports?range=custom&start_date={future.isoformat()}&end_date={future.isoformat()}"
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("No transactions match these filters", response.get_data(as_text=True))


if __name__ == "__main__":
    unittest.main()
