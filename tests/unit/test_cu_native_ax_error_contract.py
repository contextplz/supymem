"""Pinned native source wiring regression; pure rustc tests run by candidate builder."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_native_ax_error_is_typed_and_has_no_second_effect_fallback():
    patch = (ROOT / 'scripts/desktop/vendor/cua-modal-0.29.1/exact-modal.patch').read_text()
    assert 'AxActionError::from_return(err)' in patch
    assert 'ax_action_error_result(&e)' in patch
    assert '+        anyhow::bail!("AXUIElementPerformAction({ax_action}) returned {err}");' not in patch
    assert '+    fn typed_ax_error_receipt_is_unconfirmed_and_private()' in patch
