import pytest
from anycam2rtsp.host import parse_default_gateway, HostDiscoveryError

REAL = "default via 172.30.64.1 dev eth0 proto kernel \n"


def test_parses_gateway_from_real_wsl_output():
    assert parse_default_gateway(REAL) == "172.30.64.1"


def test_parses_when_multiple_routes_present():
    out = ("default via 172.30.64.1 dev eth0 proto kernel \n"
           "172.30.64.0/20 dev eth0 proto kernel scope link src 172.30.70.5\n")
    assert parse_default_gateway(out) == "172.30.64.1"


def test_raises_when_no_default_route():
    with pytest.raises(HostDiscoveryError, match="no default route"):
        parse_default_gateway("172.30.64.0/20 dev eth0 scope link\n")


def test_raises_on_empty_output():
    with pytest.raises(HostDiscoveryError):
        parse_default_gateway("")
