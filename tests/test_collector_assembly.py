import pytest
import torch

from test_collector_list_inputs import _load_collector_module


def _collector():
    return _load_collector_module().DistributedCollectorNode()


def _frame(value, size=2):
    return torch.full((1, size, size, 3), value, dtype=torch.uint8)


def test_worker_frames_are_released_during_assembly():
    collector = _collector()
    worker_images = {"w1": {0: _frame(0), 1: _frame(255)}}

    combined = collector._reorder_and_combine_tensors(worker_images, ["w1"], 0, None, False, None)

    assert combined.shape == (2, 2, 2, 3)
    assert combined.dtype == torch.float32
    assert worker_images["w1"] == {}


def test_uint8_frames_match_float_conversion():
    collector = _collector()
    source = torch.arange(2 * 2 * 3, dtype=torch.uint8).reshape(1, 2, 2, 3)

    combined = collector._reorder_and_combine_tensors({"w1": {0: source.clone()}}, ["w1"], 0, None, False, None)

    assert torch.equal(combined, source.float() / 255.0)


def test_master_frames_precede_workers_in_configured_order():
    collector = _collector()
    master = torch.full((1, 2, 2, 3), 0.5)
    worker_images = {"w2": {0: _frame(2)}, "w1": {0: _frame(1)}}

    combined = collector._reorder_and_combine_tensors(worker_images, ["w1", "w2"], 1, master, False, None)

    assert combined.shape[0] == 3
    assert torch.equal(combined[0], master[0])
    assert float(combined[1].max()) == pytest.approx(1 / 255)
    assert float(combined[2].max()) == pytest.approx(2 / 255)


def test_falls_back_when_no_frames_were_collected():
    collector = _collector()
    fallback = torch.zeros((1, 2, 2, 3))

    assert torch.equal(collector._reorder_and_combine_tensors({}, [], 0, None, True, fallback), fallback)
    assert collector._reorder_and_combine_tensors({}, [], 0, None, True, None) is None
