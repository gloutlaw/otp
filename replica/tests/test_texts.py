from datetime import datetime

import replica.billing_ui as billing_ui
import replica.telegram as telegram
from replica.billing.entitlements import Status
from replica.quota import LOCAL_TZ
from replica.screens import PURCHASE_PRO_LABEL, PURCHASE_START_LABEL, SCREENS, render

LEAK_MARKERS = ("OPENROUTER", "OpenRouter", ".env", "бесплатной модели", "Ключ OpenRouter", "Задайте", "Лимит бесплатной")
NOW = datetime(2026, 10, 14, 12, tzinfo=LOCAL_TZ)


def _billing_texts() -> list[str]:
    names = [name for name in dir(billing_ui) if name.startswith("TEXT_")]
    texts = [getattr(billing_ui, name) for name in names]
    texts.append(billing_ui.consent_text("pro", upgrading_from_start=True))
    texts.append(billing_ui.paywall_text())
    texts.append(billing_ui.denied_text("cheap", True, NOW, 10, 0, NOW))
    texts.append(billing_ui.denied_text("cheap", False, NOW, 500, 0, NOW))
    texts.append(billing_ui.denied_text("smart", False, NOW, 150, 0, NOW))
    for kind in ("reminder", "failed", "expired"):
        texts.append(billing_ui.notice_text(kind, "pro", NOW))
    return texts


def test_rendered_screens_do_not_leak_configuration() -> None:
    for screen_id in SCREENS:
        text = render(screen_id, bot_name="MewTon", support_contact="@support")
        for marker in LEAK_MARKERS:
            assert marker not in text, (screen_id, marker)


def test_billing_and_chat_texts_do_not_leak_configuration() -> None:
    for text in _billing_texts() + [telegram.RATE_LIMITED_TEXT, telegram.UNAVAILABLE_TEXT]:
        for marker in LEAK_MARKERS:
            assert marker not in text, (text, marker)


def _status(tier: str, subscribed: bool) -> Status:
    return Status(
        tier=tier, tier_until=None, auto_renew=False, subscribed=subscribed,
        cheap=None, smart=None, packs=(), trial_left=0,
    )


def test_start_purchase_stays_visible_without_a_paid_subscription() -> None:
    hidden = billing_ui.hidden_labels("tariffs", _status("start", subscribed=False))
    assert PURCHASE_START_LABEL not in hidden
    assert PURCHASE_PRO_LABEL not in hidden


def test_paid_start_hides_only_the_same_plan() -> None:
    hidden = billing_ui.hidden_labels("tariffs", _status("start", subscribed=True))
    assert PURCHASE_START_LABEL in hidden
    assert PURCHASE_PRO_LABEL not in hidden


def test_dispatcher_registers_payment_and_message_handlers() -> None:
    dispatcher = telegram.make_dispatcher("MewTon", "@support", "key")
    assert len(dispatcher.pre_checkout_query.handlers) == 1
    assert len(dispatcher.subscription.handlers) == 1
    assert len(dispatcher.message.handlers) == 3
    assert len(dispatcher.callback_query.handlers) == 1
