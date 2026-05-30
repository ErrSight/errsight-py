import errsight


def test_version_is_set():
    assert isinstance(errsight.__version__, str)
    assert errsight.__version__
