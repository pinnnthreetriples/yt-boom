import asyncio
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from pydantic import ValidationError

from config import (
    AccountSettings,
    AppConfig,
    AppSettings,
    BrowserSettings,
    CollectorEndpoint,
    CollectorSettings,
    DeepSeekSettings,
    LimitsSettings,
    ProcessorSettings,
    TargetSettings,
    ValidatorSettings,
    parse_proxy,
)
from modules.interaction_simulator import InteractionSimulator
from modules.youtube_runner import YouTubeRunner


class TestMultiAccountRunner(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.session_root = Path(self.temp_dir.name)
        self.browser_cfg = BrowserSettings(
            headless=True,
            session_root=self.session_root,
            render_min_seconds=0.1,
            render_max_seconds=0.2,
            targets=[
                TargetSettings(
                    name="youtube",
                    base_url="https://www.youtube.com",
                    item_url_template="https://www.youtube.com/watch?v={external_id}",
                    submit_input_selector="#reply-dialog #contenteditable-root",
                    submit_button_selector="#reply-dialog #submit-button button",
                    confirmation_selector="ytd-comment-view-model",
                )
            ],
        )
        fingerprint_patch = patch("modules.youtube_runner.get_or_create_fingerprint", return_value={"pinned": True})
        fingerprint_patch.start()  # _launch_options must not generate a real fingerprint (0.4 s, needs the fpgen data)
        self.addCleanup(fingerprint_patch.stop)
        self.target = self.browser_cfg.targets[0]
        self.runner = YouTubeRunner(self.browser_cfg, self.target)

    def tearDown(self):
        self.temp_dir.cleanup()

    # ------------------------------------------------------------- Session Dir

    def test_dynamic_session_dir_override(self):
        # 1. Default session directory
        default_runner = YouTubeRunner(self.browser_cfg, self.target)
        self.assertEqual(default_runner.session_dir, self.session_root / "youtube")
        self.assertEqual(default_runner.account_name, "default")
        opts_default = default_runner._launch_options()
        self.assertEqual(opts_default["user_data_dir"], str(self.session_root / "youtube"))

        # 2. Custom session directory as Path
        custom_path = self.session_root / "custom_account_alpha"
        alpha_runner = YouTubeRunner(
            self.browser_cfg,
            self.target,
            session_dir=custom_path,
            account_name="account_alpha",
        )
        self.assertEqual(alpha_runner.session_dir, custom_path)
        self.assertEqual(alpha_runner.account_name, "account_alpha")
        opts_alpha = alpha_runner._launch_options()
        self.assertEqual(opts_alpha["user_data_dir"], str(custom_path))

        # 3. Custom session directory as string
        str_path = str(self.session_root / "custom_account_beta")
        beta_runner = YouTubeRunner(
            self.browser_cfg,
            self.target,
            session_dir=str_path,
            account_name="account_beta",
        )
        self.assertEqual(beta_runner.session_dir, Path(str_path))
        self.assertEqual(beta_runner.account_name, "account_beta")
        opts_beta = beta_runner._launch_options()
        self.assertEqual(opts_beta["user_data_dir"], str_path)

    # ----------------------------------------------------------- Proxy Override

    def test_proxy_override_and_launch_options(self):
        # 1. Base browser config has proxy, runner inherits it by default
        self.browser_cfg.proxy = "http://base_proxy:8080"
        inherited_runner = YouTubeRunner(self.browser_cfg, self.target)
        self.assertEqual(inherited_runner.proxy, "http://base_proxy:8080")
        opts_inherited = inherited_runner._launch_options()
        self.assertEqual(opts_inherited["proxy"], {"server": "http://base_proxy:8080"})

        # 2. Runner overrides proxy with credentials
        override_proxy = "http://myuser:secret123@override_proxy:9090"
        custom_runner = YouTubeRunner(
            self.browser_cfg,
            self.target,
            proxy=override_proxy,
            account_name="proxied_acc",
        )
        self.assertEqual(custom_runner.proxy, override_proxy)
        opts_custom = custom_runner._launch_options()
        self.assertEqual(
            opts_custom["proxy"],
            {"server": "http://override_proxy:9090", "username": "myuser", "password": "secret123"},
        )

        # 3. Runner explicitly overrides proxy to None (direct connection)
        direct_runner = YouTubeRunner(self.browser_cfg, self.target, proxy=None)
        self.assertIsNone(direct_runner.proxy)
        opts_direct = direct_runner._launch_options()
        self.assertNotIn("proxy", opts_direct)

        # 4. Invalid proxy URL raises ValueError in _launch_options
        invalid_runner = YouTubeRunner(self.browser_cfg, self.target, proxy="http://bad_proxy_no_port")
        with self.assertRaises(ValueError):
            invalid_runner._launch_options()

    def test_account_settings_model_validation(self):
        # 1. Valid defaults
        acc = AccountSettings(name="main_acc")
        self.assertEqual(acc.name, "main_acc")
        self.assertEqual(acc.session_path, "test_sessions/youtube")
        self.assertIsNone(acc.proxy_url)
        self.assertTrue(acc.is_active)
        self.assertIsNone(acc.playwright_proxy())

        # 2. Custom values
        acc_custom = AccountSettings(
            name="acc2",
            session_path="custom/path",
            proxy_url="http://user:pass@127.0.0.1:8080",
            is_active=False,
        )
        self.assertEqual(acc_custom.name, "acc2")
        self.assertEqual(acc_custom.session_path, "custom/path")
        self.assertEqual(acc_custom.proxy_url, "http://user:pass@127.0.0.1:8080")
        self.assertFalse(acc_custom.is_active)
        self.assertEqual(
            acc_custom.playwright_proxy(),
            {"server": "http://127.0.0.1:8080", "username": "user", "password": "pass"},
        )

        # 3. Empty name raises ValidationError
        with self.assertRaises(ValidationError):
            AccountSettings(name="")

    def test_browser_settings_default_account_backward_compatibility(self):
        # When accounts is omitted in BrowserSettings, default account is automatically injected
        b_cfg = BrowserSettings(
            headless=True,
            session_root=self.session_root,
            targets=[TargetSettings(name="youtube", base_url="https://www.youtube.com", submit_input_selector="i", submit_button_selector="b", confirmation_selector="c")],
        )
        self.assertEqual(len(b_cfg.accounts), 1)
        self.assertEqual(b_cfg.accounts[0].name, "default")
        self.assertTrue(b_cfg.accounts[0].is_active)
        self.assertTrue(b_cfg.accounts[0].session_path.endswith("youtube"))

    def test_app_config_accounts_sync(self):
        app_s = AppSettings()
        ds_s = DeepSeekSettings(api_key="sk-test")
        col_s = CollectorSettings(
            endpoints=[CollectorEndpoint(name="ep", url="https://example.com/api")]
        )
        proc_s = ProcessorSettings(system_prompt="sys", user_prompt_template="user {text}")
        lim_s = LimitsSettings()
        val_s = ValidatorSettings(endpoint_template="https://example.com/{remote_id}")

        # 1. Neither accounts nor browser.accounts specified: both get default account
        b_cfg1 = BrowserSettings(
            headless=True,
            targets=[TargetSettings(name="youtube", base_url="https://www.youtube.com", submit_input_selector="i", submit_button_selector="b", confirmation_selector="c")],
        )
        cfg1 = AppConfig(
            app=app_s,
            deepseek=ds_s,
            collector=col_s,
            processor=proc_s,
            browser=b_cfg1,
            limits=lim_s,
            validator=val_s,
        )
        self.assertEqual(len(cfg1.accounts), 1)
        self.assertEqual(cfg1.accounts[0].name, "default")
        self.assertEqual(cfg1.browser.accounts, cfg1.accounts)

        # 2. Accounts provided at top-level AppConfig: synced to browser.accounts
        b_cfg2 = BrowserSettings(
            headless=True,
            targets=[TargetSettings(name="youtube", base_url="https://www.youtube.com", submit_input_selector="i", submit_button_selector="b", confirmation_selector="c")],
        )
        custom_accounts = [
            AccountSettings(name="acc_1", session_path="sessions/acc1"),
            AccountSettings(name="acc_2", session_path="sessions/acc2", proxy_url="http://p:8080"),
        ]
        cfg2 = AppConfig(
            app=app_s,
            deepseek=ds_s,
            collector=col_s,
            processor=proc_s,
            browser=b_cfg2,
            limits=lim_s,
            validator=val_s,
            accounts=custom_accounts,
        )
        self.assertEqual(len(cfg2.accounts), 2)
        self.assertEqual(cfg2.browser.accounts, cfg2.accounts)
        self.assertEqual(cfg2.browser.accounts[0].name, "acc_1")
        self.assertEqual(cfg2.browser.accounts[1].name, "acc_2")


if __name__ == "__main__":
    unittest.main()
