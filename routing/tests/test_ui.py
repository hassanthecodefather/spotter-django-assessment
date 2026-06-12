from django.test import TestCase


class TestIndexPage(TestCase):
    def test_get_index_returns_200(self):
        resp = self.client.get("/")
        self.assertEqual(resp.status_code, 200)

    def test_index_contains_form(self):
        resp = self.client.get("/")
        content = resp.content.decode()
        self.assertIn('<form', content)
        self.assertIn('id="start"', content)
        self.assertIn('id="finish"', content)

    def test_index_has_json_script_hook(self):
        # The page must have a json_script tag for the CSRF mechanism.
        # We check that the DTL json_script filter was used (produces <script type="application/json">).
        resp = self.client.get("/")
        content = resp.content.decode()
        self.assertIn('application/json', content)
