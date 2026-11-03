import unittest
from unittest.mock import Mock, patch

import trueface_poller


class _SlowForm:
    """Stands in for WebDriverWait: fails until the page has been loaded twice.

    The device draws its login form from a JS bundle that can take longer than
    one wait to render on a cold cache, so the first wait times out and the
    login must reload before deciding the device is unreachable.
    """

    def __init__(self, driver, loads, ready_after_loads):
        self._driver = driver
        self._loads = loads
        self._ready_after_loads = ready_after_loads

    def until(self, _condition):
        if self._loads[0] < self._ready_after_loads:
            raise TimeoutError("")
        return Mock()


class TrueFaceLoginRetryTests(unittest.TestCase):
    def _driver(self, loads):
        driver = Mock()
        driver.current_url = "http://device/#/login"
        driver.page_source = "<html></html>"
        driver.get = Mock(side_effect=lambda _url: loads.__setitem__(0, loads[0] + 1))
        button = Mock()
        button.text = "Login"
        driver.find_elements = Mock(return_value=[button])
        return driver

    def test_login_reloads_until_the_form_renders(self):
        loads = [0]
        driver = self._driver(loads)

        with (
            patch.object(
                trueface_poller,
                "LOGIN_FORM_ATTEMPTS",
                3,
            ),
            patch(
                "selenium.webdriver.support.ui.WebDriverWait",
                lambda d, _t: _SlowForm(d, loads, ready_after_loads=2),
            ),
            patch.object(trueface_poller.time, "sleep", lambda _s: None),
        ):
            self.assertTrue(trueface_poller._login(driver))

        self.assertEqual(driver.get.call_count, 2)

    def test_login_gives_up_after_the_configured_attempts(self):
        loads = [0]
        driver = self._driver(loads)

        with (
            patch.object(trueface_poller, "LOGIN_FORM_ATTEMPTS", 2),
            patch(
                "selenium.webdriver.support.ui.WebDriverWait",
                lambda d, _t: _SlowForm(d, loads, ready_after_loads=99),
            ),
            patch.object(trueface_poller.time, "sleep", lambda _s: None),
        ):
            self.assertFalse(trueface_poller._login(driver))

        self.assertEqual(driver.get.call_count, 2)

    def test_login_waits_at_least_as_long_as_the_old_fifteen_seconds(self):
        self.assertGreaterEqual(trueface_poller.LOGIN_FORM_WAIT_SECONDS, 15)

    def test_chrome_profile_is_kept_on_disk_beside_the_poller(self):
        self.assertTrue(trueface_poller.CHROME_PROFILE_DIR)
        self.assertIn("chrome_profile", trueface_poller.CHROME_PROFILE_DIR)


if __name__ == "__main__":
    unittest.main()


class TrueFacePageLoadTimeoutTests(unittest.TestCase):
    def test_page_load_is_allowed_to_take_as_long_as_the_form_wait(self):
        # Chrome aborts a load that outlives its page-load timeout, so a 30s
        # cap would kill the load before the 60s form wait ever started.
        self.assertGreaterEqual(
            trueface_poller.PAGE_LOAD_TIMEOUT_SECONDS,
            trueface_poller.LOGIN_FORM_WAIT_SECONDS,
        )

    def test_driver_applies_the_page_load_timeout_it_configures(self):
        driver = Mock()
        with patch("selenium.webdriver.Chrome", Mock(return_value=driver)):
            self.assertIs(trueface_poller._create_driver(), driver)

        driver.set_page_load_timeout.assert_called_once_with(
            trueface_poller.PAGE_LOAD_TIMEOUT_SECONDS
        )
