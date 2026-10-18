"""Allowlisted Prometheus hwmon joins, with no real scraping or hardware IO."""

import re

import pytest

from plugins.temperatures.node_exporter_temperatures import parse_node_exporter_temperatures as parse
from plugins.temperatures.temperatures import validate_temperatures


NODE, TS = "pve-blsy-1", 1_792_257_600
INPUT = 'node_hwmon_temp_celsius{chip="platform_coretemp_0",sensor="temp1"} '
SUCCESS = 'node_scrape_collector_success{collector="hwmon"} 1\n'


def sample(value="0", *, chip="platform_coretemp_0", name="coretemp", sensor="temp1", label="Package id 0"):
    return '\n'.join([
        f'node_hwmon_chip_names{{chip="{chip}",chip_name="{name}"}} 1',
        f'node_hwmon_sensor_label{{chip="{chip}",sensor="{sensor}",label="{label}"}} 1',
        f'node_hwmon_temp_celsius{{chip="{chip}",sensor="{sensor}"}} {value}',
        f'node_hwmon_temp_max_celsius{{chip="{chip}",sensor="{sensor}"}} 90',
        f'node_hwmon_temp_crit_celsius{{chip="{chip}",sensor="{sensor}"}} 105',
    ])


def test_intel_package_zero_and_driver_limits_join_in_any_order_with_fetch_timestamp():
    text = '\n'.join(reversed(sample().splitlines()))
    data = parse(text, NODE, TS)
    assert data == validate_temperatures(data, NODE, TS)
    row = data["sensors"][0]
    assert data["sampled_at"] == TS and row["celsius"] == 0
    assert row["chip"] == "coretemp [platform_coretemp_0]" and row["label"] == "Package id 0"
    assert row["category"] == "cpu" and row["max_celsius"] == 90 and row["critical_celsius"] == 105
    assert re.fullmatch(r"[A-Za-z0-9_.:-]+", row["id"])


def test_amd_tdie_tctl_and_ccd_labels_are_retained_without_guessing_offset_or_thresholds():
    lines = ['node_hwmon_chip_names{chip="pci0000:00_0000:00:18_3",chip_name="k10temp"} 1']
    for number, label, value in [(1, "Tctl", 76.25), (2, "Tdie", 56.25), (3, "Tccd1", 52.5)]:
        lines.extend([f'node_hwmon_sensor_label{{chip="pci0000:00_0000:00:18_3",sensor="temp{number}",label="{label}"}} 1',
                      f'node_hwmon_temp_celsius{{chip="pci0000:00_0000:00:18_3",sensor="temp{number}"}} {value}'])
    rows = parse('\n'.join(lines), NODE, TS)["sensors"]
    assert [(row["label"], row["celsius"]) for row in rows] == [("Tctl", 76.25), ("Tdie", 56.25), ("Tccd1", 52.5)]
    assert all(row["category"] == "cpu" and row["max_celsius"] is row["critical_celsius"] is None for row in rows)


@pytest.mark.parametrize("driver,category", [("coretemp", "cpu"), ("k10temp", "cpu"), ("zenpower", "cpu"),
    ("nct6798", "board"), ("it8628", "board"), ("nvme", "disk"), ("drivetemp", "disk"), ("acpitz", "other")])
def test_known_chip_categories_are_derived_from_annotation(driver, category):
    assert parse(sample(name=driver), NODE, TS)["sensors"][0]["category"] == category


def test_missing_chip_annotation_does_not_guess_cpu_from_temp1_or_package_label():
    text = INPUT + '42\nnode_hwmon_sensor_label{chip="platform_coretemp_0",sensor="temp1",label="CPU package"} 1'
    row = parse(text, NODE, TS)["sensors"][0]
    assert row["category"] == "other" and row["label"] == "CPU package" and row["chip"] == "platform_coretemp_0"


def test_same_named_chips_keep_unique_device_names_and_ids_and_labels_do_not_define_identity():
    rows = parse(sample(chip="device0") + '\n' + sample(chip="device1"), NODE, TS)["sensors"]
    assert len({row["id"] for row in rows}) == len({row["chip"] for row in rows}) == 2
    changed = parse(sample("44", chip="device0", label="Renamed package"), NODE, TS)["sensors"][0]
    assert changed["id"] == rows[0]["id"]


def test_prometheus_label_escaping_utf8_trailing_comma_and_scientific_value():
    text = '''node_hwmon_chip_names{chip="cpu0",chip_name="coretemp"} 1
node_hwmon_sensor_label{chip="cpu0",sensor="temp1",label="核心 \\"A\\"\\\\B\\nPackage",} 1
node_hwmon_temp_celsius{chip="cpu0",sensor="temp1",} +4.25e+1 1792257000000
'''
    data = parse(text, NODE, TS)
    row = data["sensors"][0]
    assert row["label"] == '核心 "A"\\B\\nPackage' and row["celsius"] == 42.5
    assert data["sampled_at"] == TS
    assert validate_temperatures(data, NODE, TS) == data


def test_comments_and_unrelated_metrics_with_nan_histograms_or_unparseable_values_are_ignored():
    text = '''# HELP node_hwmon_temp_celsius Temperature
# TYPE node_hwmon_temp_celsius gauge
# random comment
node_cpu_seconds_total{cpu="0",mode="idle"} NaN
node_network_receive_bytes_total{device="eth0"} +Inf
http_request_duration_seconds_bucket{le="+Inf"} 42
unrelated_metric even-this-is-not-a-float
node_hwmon_fan_rpm{chip="board",sensor="fan1"} NaN
node_hwmon_temp_crit_alarm_celsius{chip="cpu",sensor="temp1"} 0.001
node_scrape_collector_success{collector="cpu"} 0
'''
    assert parse(text + sample(), NODE, TS)["sensors"][0]["celsius"] == 0


@pytest.mark.parametrize("value", ["NaN", "+Inf", "-Inf", "1e999", "-100.1", "250.1"])
def test_nonfinite_or_out_of_range_actual_readings_are_unknown_not_zero(value):
    assert parse(INPUT + value, NODE, TS)["sensors"] == []


@pytest.mark.parametrize("value", ["-100", "0", "250", ".25", "25."])
def test_valid_finite_boundaries_and_decimal_spellings(value):
    assert parse(INPUT + value, NODE, TS)["sensors"][0]["celsius"] == float(value)


def test_nonfinite_limits_are_null_and_zero_limit_is_real():
    text = sample().replace('} 90', '} NaN').replace('} 105', '} 0')
    row = parse(text, NODE, TS)["sensors"][0]
    assert row["max_celsius"] is None and row["critical_celsius"] == 0


@pytest.mark.parametrize("metric,value", [("node_hwmon_temp_fault", "1"), ("node_hwmon_temp_fault", "-1"),
    ("node_hwmon_temp_fault", "NaN"), ("node_hwmon_temp_enable_celsius", "0"),
    ("node_hwmon_temp_enable_celsius", "NaN"), ("node_hwmon_temp_enable", "0")])
def test_faulted_disabled_or_unknown_flags_suppress_channel(metric, value):
    text = INPUT + '43\n' + metric + '{chip="platform_coretemp_0",sensor="temp1"} ' + value
    assert parse(text, NODE, TS)["sensors"] == []


@pytest.mark.parametrize("metric,value", [("node_hwmon_temp_fault", "0"), ("node_hwmon_temp_enable_celsius", "0.001"),
    ("node_hwmon_temp_enable", "1")])
def test_official_scaled_enable_and_unscaled_fault_flags_are_handled(metric, value):
    text = INPUT + '0\n' + metric + '{chip="platform_coretemp_0",sensor="temp1"} ' + value
    assert parse(text, NODE, TS)["sensors"][0]["celsius"] == 0


@pytest.mark.parametrize("extra", [INPUT + '0',
    'node_hwmon_temp_max_celsius{chip="platform_coretemp_0",sensor="temp1"} 90',
    'node_hwmon_chip_names{chip="platform_coretemp_0",chip_name="coretemp"} 1',
    'node_hwmon_chip_names{chip="platform_coretemp_0",chip_name="k10temp"} 1',
    'node_hwmon_sensor_label{chip="platform_coretemp_0",sensor="temp1",label="different"} 1'])
def test_duplicate_sensor_samples_or_conflicting_metadata_fail_closed(extra):
    with pytest.raises(ValueError, match="^invalid_node_exporter_response$"):
        parse(sample() + '\n' + extra, NODE, TS)


@pytest.mark.parametrize("text", [INPUT + 'true', INPUT + '40 trailing-garbage', INPUT + '40 12.5',
    'node_hwmon_temp_celsius{chip="cpu",chip="cpu",sensor="temp1"} 40',
    'node_hwmon_temp_celsius{chip="cpu",sensor="temp1",job="node"} 40',
    'node_hwmon_temp_celsius{chip="cpu"} 40',
    'node_hwmon_temp_celsius{chip="cpu",sensor="fan1"} 40',
    'node_hwmon_temp_celsius{chip="cpu",sensor="temp1"}40',
    'node_hwmon_sensor_label{chip="cpu",sensor="temp1",label="bad\\tvalue"} 1',
    'node_hwmon_sensor_label{chip="cpu",sensor="temp1",label="unterminated} 1',
    'node_hwmon_chip_names{chip="cpu",chip_name="coretemp"} NaN',
    'node_hwmon_chip_names{chip="cpu",chip_name="coretemp"} 0'])
def test_malformed_allowlisted_samples_have_only_fixed_error(text):
    with pytest.raises(ValueError) as error:
        parse(text, NODE, TS)
    assert str(error.value) == 'invalid_node_exporter_response'


def test_empty_labels_fall_back_to_sensor_and_fan_metadata_does_not_create_fake_temperatures():
    text = INPUT + '40\nnode_hwmon_sensor_label{chip="platform_coretemp_0",sensor="temp1",label=""} 1\n'
    text += 'node_hwmon_sensor_label{chip="board",sensor="fan1",label="CPU Fan"} 1\n'
    assert parse(text, NODE, TS)["sensors"][0]["label"] == "temp1"
    assert parse('node_hwmon_sensor_label{chip="board",sensor="fan1",label="CPU Fan"} 1', NODE, TS)["sensors"] == []


@pytest.mark.parametrize("text", ['', 'OK', '<html>Upstream error</html>', 'up 1',
    'node_scrape_collector_success{collector="cpu"} 1', 'node_scrape_collector_success{collector="hwmon"} 0',
    'node_scrape_collector_success{collector="hwmon"} NaN'])
def test_missing_or_failed_hwmon_is_unavailable_instead_of_no_sensors(text):
    with pytest.raises(ValueError, match="^invalid_node_exporter_response$"):
        parse(text, NODE, TS)


def test_success_marker_or_chip_metadata_can_establish_a_genuinely_empty_source():
    assert parse(SUCCESS, NODE, TS)["sensors"] == []
    assert parse('node_hwmon_chip_names{chip="board",chip_name="nct6798"} 1', NODE, TS)["sensors"] == []
    with pytest.raises(ValueError):
        parse(SUCCESS + SUCCESS, NODE, TS)


def test_sensor_count_matching_sample_count_text_length_and_body_size_are_bounded():
    lines = [f'node_hwmon_temp_celsius{{chip="cpu",sensor="temp{number}"}} 0' for number in range(1, 257)]
    assert len(parse('\n'.join(lines), NODE, TS)["sensors"]) == 256
    with pytest.raises(ValueError):
        parse('\n'.join(lines) + '\nnode_hwmon_temp_celsius{chip="cpu",sensor="temp257"} 0', NODE, TS)
    many = [f'node_hwmon_sensor_label{{chip="board",sensor="fan{number}",label="fan"}} 1' for number in range(4097)]
    with pytest.raises(ValueError):
        parse('\n'.join(many), NODE, TS)
    with pytest.raises(ValueError):
        parse(sample(label='x' * 129), NODE, TS)
    with pytest.raises(ValueError):
        parse('#' * (4_194_304 + 1), NODE, TS)


def test_long_display_names_still_preserve_unique_chip_identifier_within_dto_length_limit():
    chip = "device_" + "x" * 100
    row = parse(sample(chip=chip, name="coretemp" + "x" * 100), NODE, TS)["sensors"][0]
    assert chip in row["chip"] and len(row["chip"]) <= 128


@pytest.mark.parametrize("node,sampled_at", [("node.example", TS), ("node", True), ("node", -1), ("node", float("nan")),
    ("node", float("inf")), ("node", 2 ** 1024)])
def test_invalid_fetch_context_has_fixed_error(node, sampled_at):
    with pytest.raises(ValueError, match="^invalid_node_exporter_response$"):
        parse(SUCCESS, node, sampled_at)
