import pytest


@pytest.fixture(autouse=True)
def isolate_job_checkpoints(monkeypatch, tmp_path):
    monkeypatch.setenv('DALA_JOB_STATE_DIR', str(tmp_path / 'jobs'))
