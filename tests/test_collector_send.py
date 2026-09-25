import asyncio

import pytest
import torch

from test_collector_list_inputs import _load_collector_module


class _FakeImage:
    def save(self, buffer, format=None, compress_level=None):
        buffer.write(b"png-bytes")


class _FakePost:
    def __init__(self, session, payload):
        self._session = session
        self._payload = payload

    async def __aenter__(self):
        self._session.events.append("post")
        self._session.payloads.append(self._payload)
        return self

    async def __aexit__(self, *_exc_info):
        return False

    def raise_for_status(self):
        return None


class _FakeSession:
    def __init__(self):
        self.events = []
        self.payloads = []

    def post(self, _url, json=None, timeout=None):
        return _FakePost(self, json)


def _worker(session):
    module = _load_collector_module()
    module.get_client_session = _make_session_getter(session)
    module.tensor_to_pil = _make_encoder(session)
    return module.DistributedCollectorNode()


def _make_session_getter(session):
    async def _get_client_session():
        return session

    return _get_client_session


def _make_encoder(session):
    def _tensor_to_pil(_batch, _index):
        session.events.append("encode")
        return _FakeImage()

    return _tensor_to_pil


def _send(worker, image_batch, audio=None):
    return asyncio.run(
        worker.send_batch_to_master(image_batch, audio, "job-1", "http://master", "w1")
    )


def test_each_frame_is_encoded_only_once_its_turn_to_send_arrives():
    session = _FakeSession()

    _send(_worker(session), torch.zeros(3, 1, 1, 3))

    assert session.events == ["encode", "post", "encode", "post", "encode", "post"]


def test_final_frame_is_flagged_and_carries_audio():
    session = _FakeSession()

    _send(_worker(session), torch.zeros(2, 1, 1, 3), audio={"waveform": torch.ones(1, 2, 2)})

    assert [payload["batch_idx"] for payload in session.payloads] == [0, 1]
    assert [payload["is_last"] for payload in session.payloads] == [False, True]
    assert "audio" not in session.payloads[0]
    assert "audio" in session.payloads[1]


def test_completion_without_image_or_audio_is_rejected_before_sending():
    session = _FakeSession()

    with pytest.raises(ValueError):
        _send(_worker(session), None)

    assert session.events == []
