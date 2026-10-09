from app.registers import REGISTERS, READ_BLOCKS, decode, derive, encode, extract


def test_decode_signed_and_unsigned():
    assert decode("INT32", encode("INT32", -2500)) == -2500
    assert decode("INT32", [0x0000, 0x0BB8]) == 3000
    assert decode("UINT32", [0x0001, 0x0000]) == 65536
    assert decode("INT16", [0xFFFF]) == -1
    assert decode("STRING", encode("STRING", "A17C5", 5)) == "A17C5"


def test_every_register_is_inside_a_read_block():
    for reg in REGISTERS:
        assert any(
            kind == reg.kind and start <= reg.address and reg.address + reg.count - 1 <= end
            for kind, start, end in READ_BLOCKS
        ), reg.key


def test_extract_and_derive():
    words = [0] * 51
    words[2:4] = encode("INT32", 1800)  # pv pcs @10002
    words[4:6] = encode("INT32", 200)  # third-party pv @10004
    words[8:10] = encode("INT32", -1500)  # battery charging @10008
    words[10:12] = encode("INT32", 450)  # load @10010
    words[12:14] = encode("INT32", -50)  # grid export @10012
    words[14] = 73  # soc @10014
    words[18:20] = encode("UINT32", 1234)  # pv total, gain 10 @10018
    words[1] = 1  # charging
    raw = extract({("input", 10000): words, ("holding", 10064): [6]})
    snap = derive(raw)
    assert snap["solar_w"] == 2000
    assert snap["battery_w"] == -1500
    assert snap["grid_w"] == -50
    assert snap["soc"] == 73
    assert snap["solar_total_kwh"] == 123.4
    assert snap["battery_status"] == "charging"
    assert snap["operating_mode"] == "Smart"
    # Blocks that were not read leave their values empty rather than zero.
    assert snap["ac_output_w"] is None
    assert snap["serial"] is None
