import os
import shutil
import unittest

import app as app_module


class TriatlonAppTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = os.path.join(app_module.BASE_DIR, "tests_tmp")
        os.makedirs(self.tempdir, exist_ok=True)
        self.db_path = os.path.join(self.tempdir, "test.sqlite3")
        if os.path.exists(self.db_path):
            os.remove(self.db_path)
        self.app = app_module.app
        self.app.config.update(
            TESTING=True,
            DATABASE=self.db_path,
            SECRET_KEY="test-secret",
        )
        with self.app.app_context():
            app_module.init_db()
        self.client = self.app.test_client()

    def tearDown(self):
        shutil.rmtree(self.tempdir, ignore_errors=True)

    def create_event(
        self,
        title,
        slug,
        event_at="2026-04-10 18:00:00",
        deadline="2026-04-10 12:00:00",
        has_fee=0,
        fee_amount=0,
        beneficiary_name="",
        bank_account="",
    ):
        with self.app.app_context():
            cur = app_module.execute(
                """
                INSERT INTO events (
                    title, slug, description, event_at, registration_deadline, created_at, is_closed,
                    has_fee, fee_amount, beneficiary_name, bank_account
                )
                VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?)
                """,
                (
                    title,
                    slug,
                    f"{title} leírás",
                    event_at,
                    deadline,
                    "2026-03-21 10:00:00",
                    has_fee,
                    fee_amount,
                    beneficiary_name,
                    bank_account,
                ),
            )
            return cur.lastrowid

    def create_registration(
        self,
        event_id,
        name,
        email,
        assigned_team=None,
        assigned_slot=None,
    ):
        with self.app.app_context():
            cur = app_module.execute(
                """
                INSERT INTO registrations (
                    event_id, participant_name, participant_email, provider, created_at,
                    assigned_team, assigned_stage, assigned_slot,
                    payment_status, payment_method
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event_id,
                    name,
                    email,
                    "email",
                    "2026-03-21 10:00:00",
                    assigned_team,
                    2 if assigned_team is not None else None,
                    assigned_slot,
                    app_module.PAYMENT_PAID,
                    app_module.PAYMENT_METHOD_NONE,
                ),
            )
            return cur.lastrowid

    def test_home_lists_multiple_events(self):
        self.create_event("Tavaszi Kupa", "tavaszi-kupa")
        self.create_event("Nyári Kupa", "nyari-kupa")

        response = self.client.get("/")

        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn("Tavaszi Kupa", html)
        self.assertIn("Nyári Kupa", html)
        self.assertIn("/e/tavaszi-kupa", html)
        self.assertIn("/e/nyari-kupa", html)

    def test_event_page_uses_slug_route(self):
        self.create_event("Tavaszi Kupa", "tavaszi-kupa")

        response = self.client.get("/e/tavaszi-kupa")

        self.assertEqual(response.status_code, 200)
        self.assertIn("Tavaszi Kupa", response.get_data(as_text=True))

    def test_registration_is_scoped_to_selected_event(self):
        first_event_id = self.create_event("Tavaszi Kupa", "tavaszi-kupa")
        second_event_id = self.create_event("Nyári Kupa", "nyari-kupa")

        response = self.client.post(
            "/e/tavaszi-kupa/register",
            data={"name": "Teszt Elek", "email": "teszt@example.com"},
            follow_redirects=True,
        )

        self.assertEqual(response.status_code, 200)
        with self.app.app_context():
            first_count = app_module.get_registration_count(first_event_id)
            second_count = app_module.get_registration_count(second_event_id)
        self.assertEqual(first_count, 1)
        self.assertEqual(second_count, 0)

    def test_admin_pages_work_for_specific_event(self):
        event_id = self.create_event("Admin Kupa", "admin-kupa")

        with self.client.session_transaction() as session:
            session["admin_logged_in"] = True

        dashboard = self.client.get("/admin")
        event_page = self.client.get(f"/admin/events/{event_id}")

        self.assertEqual(dashboard.status_code, 200)
        self.assertEqual(event_page.status_code, 200)
        self.assertIn("Admin Kupa", dashboard.get_data(as_text=True))
        self.assertIn("Admin Kupa", event_page.get_data(as_text=True))

    def test_admin_can_delete_event_after_confirmation(self):
        event_id = self.create_event("T?rlend? Kupa", "torlendo-kupa")

        with self.app.app_context():
            app_module.execute(
                """
                INSERT INTO registrations (
                    event_id, participant_name, participant_email, provider, created_at,
                    payment_status, payment_method
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event_id,
                    "Teszt Elek",
                    "teszt@example.com",
                    "email",
                    "2026-03-21 10:00:00",
                    app_module.PAYMENT_PENDING,
                    app_module.PAYMENT_METHOD_TRANSFER,
                ),
            )

        with self.client.session_transaction() as session:
            session["admin_logged_in"] = True

        confirm = self.client.get(f"/admin/events/{event_id}/delete")
        self.assertEqual(confirm.status_code, 200)
        self.assertIn("T?rlend? Kupa", confirm.get_data(as_text=True))
        self.assertIn(f"/admin/events/{event_id}/delete", confirm.get_data(as_text=True))

        response = self.client.post(
            f"/admin/events/{event_id}/delete",
            follow_redirects=True,
        )

        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn("/admin/events/new", html)
        self.assertNotIn("T?rlend? Kupa", html)

        with self.app.app_context():
            self.assertIsNone(app_module.get_event(event_id))
            remaining = app_module.query_one(
                "SELECT COUNT(*) AS cnt FROM registrations WHERE event_id = ?",
                (event_id,),
            )
            self.assertEqual(remaining["cnt"], 0)

    def test_registration_redirect_stays_on_event_page(self):
        self.create_event("Maradós Kupa", "marados-kupa")

        response = self.client.post(
            "/e/marados-kupa/register",
            data={"name": "Teszt Elek", "email": "teszt@example.com"},
            follow_redirects=True,
        )

        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn("Maradós Kupa", html)
        self.assertIn("Sikeres jelentkezés.", html)
        self.assertNotIn("Válassz eseményt", html)

    def test_registration_can_seed_initial_team_name_idea(self):
        self.create_event("Otletes Kupa", "otletes-kupa")

        response = self.client.post(
            "/e/otletes-kupa/register",
            data={
                "name": "Teszt Elek",
                "email": "teszt@example.com",
                "team_name_idea": "Villamkiflik",
            },
            follow_redirects=True,
        )

        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn("Villamkiflik", html)
        self.assertIn("1/2", html)

    def test_paid_event_requires_payment_method_and_saves_it(self):
        event_id = self.create_event(
            "Fizetős Kupa",
            "fizetos-kupa",
            has_fee=1,
            fee_amount=2500,
            beneficiary_name="Teszt Egyesület",
            bank_account="12345678-12345678-12345678",
        )

        missing_method = self.client.post(
            "/e/fizetos-kupa/register",
            data={"name": "Teszt Elek", "email": "teszt@example.com"},
            follow_redirects=True,
        )

        self.assertEqual(missing_method.status_code, 200)
        self.assertIn("Fizetős eseménynél válassz fizetési módot: utalás vagy készpénz.", missing_method.get_data(as_text=True))

        response = self.client.post(
            "/e/fizetos-kupa/register",
            data={
                "name": "Teszt Elek",
                "email": "teszt@example.com",
                "payment_method": "cash",
            },
            follow_redirects=True,
        )

        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn("Készpénz", html)
        self.assertIn("24 órával", html)

        with self.app.app_context():
            reg = app_module.query_one(
                """
                SELECT payment_status, payment_method
                FROM registrations
                WHERE event_id = ? AND participant_email = ?
                """,
                (event_id, "teszt@example.com"),
            )

        self.assertIsNotNone(reg)
        self.assertEqual(reg["payment_status"], app_module.PAYMENT_PENDING)
        self.assertEqual(reg["payment_method"], app_module.PAYMENT_METHOD_CASH)

    def test_transfer_registration_shows_personal_qr(self):
        self.create_event(
            "Utal?sos Kupa",
            "utalasos-kupa",
            has_fee=1,
            fee_amount=2500,
            beneficiary_name="Teszt Egyes?let",
            bank_account="12345678-12345678-12345678",
        )

        response = self.client.post(
            "/e/utalasos-kupa/register",
            data={
                "name": "Teszt Elek",
                "email": "teszt@example.com",
                "payment_method": "transfer",
            },
            follow_redirects=True,
        )

        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn("utal", html)
        self.assertIn("Teszt Elek", html)
        self.assertIn("0001", html)
        self.assertIn("data-copy", html)
        self.assertIn("0001", html)

    def test_first_team_name_proposal_is_visible_before_final_approval(self):
        event_id = self.create_event("Neves Kupa", "neves-kupa")
        reg1 = self.create_registration(event_id, "Teszt Elek", "teszt1@example.com", assigned_team=1, assigned_slot=1)
        self.create_registration(event_id, "Minta Bela", "teszt2@example.com", assigned_team=1, assigned_slot=2)

        with self.client.session_transaction() as session:
            session[f"my_registration_{event_id}"] = reg1

        response = self.client.post(
            "/e/neves-kupa/team-name/propose",
            data={"team_name": "Villamkiflik"},
            follow_redirects=True,
        )

        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn("Villamkiflik", html)
        self.assertIn("1/2", html)

    def test_pending_team_name_becomes_final_when_event_closes(self):
        event_id = self.create_event(
            "Lezaro Kupa",
            "lezaro-kupa",
            event_at="2026-04-10 18:00:00",
            deadline="2026-03-20 12:00:00",
        )
        reg1 = self.create_registration(event_id, "Teszt Elek", "teszt1@example.com", assigned_team=1, assigned_slot=1)
        self.create_registration(event_id, "Minta Bela", "teszt2@example.com", assigned_team=1, assigned_slot=2)

        with self.app.app_context():
            cur = app_module.execute(
                """
                INSERT INTO team_name_proposals (
                    event_id, team_number, proposed_name, proposed_by_registration_id, status, created_at, is_admin_override
                )
                VALUES (?, ?, ?, ?, 'pending', ?, 0)
                """,
                (event_id, 1, "Kesoi Nev", reg1, "2026-03-21 10:00:00"),
            )
            app_module.execute(
                """
                INSERT INTO team_name_votes (proposal_id, registration_id, vote, created_at)
                VALUES (?, ?, 'approve', ?)
                """,
                (cur.lastrowid, reg1, "2026-03-21 10:00:00"),
            )
            event_row = app_module.get_event(event_id)
            app_module.finalize_event_if_needed(event_row)
            final_name = app_module.get_approved_team_name(event_id, 1)

        self.assertEqual(final_name, "Kesoi Nev")


if __name__ == "__main__":
    unittest.main()

