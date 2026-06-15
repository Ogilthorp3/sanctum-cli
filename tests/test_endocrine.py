"""Endocrine system — the council's hormone panel + homeostatic regulator.

The LOAD-BEARING property under test is HOMEOSTASIS: the panel must be a
damped negative-feedback regulator that can NEVER run away. The endocrine
layer is itself feedback-damped by construction (it must not become an
alert-storm source — the truthful-alerts lesson: 0 false criticals).

The test expectations here are derived from CONTROL THEORY, deliberately a
DIFFERENT source than the production update rule, so the test cannot share a
wrong assumption with the implementation (Contracts-at-the-Boundary §2):

    A first-order discrete regulator   h[t+1] = h[t] + α·(setpoint − h[t]) + drive
    with leak gain 0 < α < 1 and bounded clamped drive is BIBO-stable:
      • drive = 0           → h decays GEOMETRICALLY toward setpoint (ratio 1−α)
      • drive bounded, clamp → h stays in [0,1] for ALL inputs, forever
      • a one-shot spike then silence → a single peak then monotone decay
      • cross-coupling cortisol⊣dopamine never amplifies (antagonism only damps)

The receptor contract is tested against the REAL receptor (no mocks) feeding a
REAL produced panel: a high-dopamine/low-cortisol panel MUST actually raise a
seat's effective temperature + widen diversity vs the neutral baseline, and the
neutral/absent panel MUST leave today's payload byte-identical (off-by-default).
"""

from __future__ import annotations

import math

import pytest

# Source of truth lives in the sanctum-cli package so it ships with the CLI and
# rides the existing test harness; the daemon copy is staged to ~/.sanctum.
from sanctum_cli.endocrine import gland, receptor
from sanctum_cli.endocrine.gland import (
    HORMONES,
    Panel,
    Regulator,
    Signals,
)

# ───────────────────────── homeostasis: the anti-runaway core ─────────────


class TestHomeostasisNoRunaway:
    """The structural guarantee: this regulator cannot blow up."""

    def test_leak_gain_is_a_proper_contraction(self) -> None:
        # 0 < α < 1 is the necessary+sufficient condition for the homogeneous
        # part (drive=0) to be a contraction. If α leaves (0,1) the proof breaks.
        for name in HORMONES:
            alpha = Regulator.for_hormone(name).leak
            assert 0.0 < alpha < 1.0, f"{name} leak gain {alpha} not a contraction"

    def test_silent_panel_decays_geometrically_to_setpoint(self) -> None:
        # With NO drive, distance-to-setpoint must shrink by exactly (1−α) each
        # step — the closed-form geometric decay, derived independently here.
        reg = Regulator.for_hormone("cortisol")
        sp = reg.setpoint
        h = 1.0  # start pinned high (a "stuck-high cortisol" worst case)
        d0 = abs(h - sp)
        prev_dist = d0
        # Enough steps for (1−α)^n to drop below 1e-4 of the initial gap, so the
        # geometric tail is provably exhausted, not just trending.
        n = int(math.log(1e-4) / math.log(1.0 - reg.leak)) + 5
        for k in range(1, n + 1):
            h = reg.step(h, drive=0.0)
            dist = abs(h - sp)
            # monotone non-increasing AND contracting by exactly (1−α)
            assert dist <= prev_dist + 1e-12
            if prev_dist > 1e-12:
                assert dist / prev_dist == pytest.approx(1.0 - reg.leak, abs=1e-9)
            # closed-form geometric: dist == d0·(1−α)^k
            assert dist == pytest.approx(d0 * (1.0 - reg.leak) ** k, abs=1e-9)
            prev_dist = dist
        assert h == pytest.approx(sp, abs=1e-3)  # converged

    def test_clamped_to_unit_interval_under_adversarial_drive(self) -> None:
        # Hostile input, not the happy path: hammer EVERY hormone with absurd
        # out-of-range drive (huge +, huge −, NaN-ish extremes) and assert the
        # level NEVER escapes [0,1]. This is the structural storm-proof.
        for name in HORMONES:
            reg = Regulator.for_hormone(name)
            h = reg.setpoint
            for drive in (1e6, -1e6, 99.0, -99.0, 5.0, -5.0) * 200:
                h = reg.step(h, drive=drive)
                assert 0.0 <= h <= 1.0, f"{name} escaped unit interval: {h}"

    def test_one_shot_spike_gives_single_peak_then_monotone_decay(self) -> None:
        # Acute arousal model: noradrenaline spikes on ONE incident then must
        # decay back. Exactly one local maximum, then strictly non-increasing.
        reg = Regulator.for_hormone("noradrenaline")
        h = reg.setpoint
        series = [h]
        h = reg.step(h, drive=0.9)  # the spike
        series.append(h)
        for _ in range(40):  # silence afterward
            h = reg.step(h, drive=0.0)
            series.append(h)
        peak_idx = series.index(max(series))
        # rises into the peak, never rises after it
        for i in range(peak_idx + 1, len(series)):
            assert series[i] <= series[i - 1] + 1e-12
        assert series[-1] == pytest.approx(reg.setpoint, abs=1e-2)

    def test_cross_coupling_cortisol_antagonizes_dopamine_only_damps(self) -> None:
        # Biologically exact: cortisol SUPPRESSES dopamine. The coupling must be
        # antagonistic (raising cortisol can only LOWER the dopamine the panel
        # reports) and must NOT create positive feedback (no amplification).
        sig_calm = Signals(headroom_mb=40000, alert_rate_1h=0, hour=14, creative_mode=True)
        sig_stress = Signals(headroom_mb=-30000, alert_rate_1h=40, hour=14, creative_mode=True)

        g = Regulator  # noqa: F841 — keep import meaningful for readers
        panel_calm = gland.settle(sig_calm, iterations=400)
        panel_stress = gland.settle(sig_stress, iterations=400)

        assert panel_stress.cortisol > panel_calm.cortisol
        # same creative drive, but stress must claw dopamine DOWN, never up
        assert panel_stress.dopamine < panel_calm.dopamine
        # and the antagonism cannot push dopamine out of range
        assert 0.0 <= panel_stress.dopamine <= 1.0

    def test_settling_is_idempotent_at_steady_state(self) -> None:
        # A homeostat at its fixed point must STAY there: settling an already
        # settled panel under unchanged signals moves nothing meaningfully.
        sig = Signals(headroom_mb=20000, alert_rate_1h=2, hour=11, creative_mode=False)
        p1 = gland.settle(sig, iterations=400)
        p2 = gland.settle(sig, iterations=400, start=p1)
        for name in HORMONES:
            assert getattr(p2, name) == pytest.approx(getattr(p1, name), abs=1e-3)


# ───────────────────────── signal → hormone mapping ──────────────────────


class TestSignalMapping:
    def test_memory_pressure_drives_cortisol_up(self) -> None:
        # castellan tiers (headroom 6144 / 2048 / 0) → rising cortisol drive.
        calm = gland.settle(Signals(headroom_mb=40000, alert_rate_1h=0, hour=14), 400)
        warn = gland.settle(Signals(headroom_mb=5000, alert_rate_1h=0, hour=14), 400)
        crit = gland.settle(Signals(headroom_mb=1000, alert_rate_1h=0, hour=14), 400)
        cat = gland.settle(Signals(headroom_mb=-5000, alert_rate_1h=0, hour=14), 400)
        assert calm.cortisol < warn.cortisol < crit.cortisol < cat.cortisol

    def test_alert_rate_drives_noradrenaline(self) -> None:
        quiet = gland.settle(Signals(headroom_mb=40000, alert_rate_1h=0, hour=14), 400)
        storm = gland.settle(Signals(headroom_mb=40000, alert_rate_1h=30, hour=14), 400)
        assert storm.noradrenaline > quiet.noradrenaline

    def test_night_raises_melatonin_lowers_arousal(self) -> None:
        # Force-Flow quiet-hours grammar: hour>=22 or hour<8 = night.
        day = gland.settle(Signals(headroom_mb=40000, alert_rate_1h=0, hour=14), 400)
        night = gland.settle(Signals(headroom_mb=40000, alert_rate_1h=0, hour=2), 400)
        assert night.melatonin > day.melatonin

    def test_creative_mode_elevates_dopamine_lowers_cortisol(self) -> None:
        base = gland.settle(Signals(headroom_mb=20000, alert_rate_1h=1, hour=14), 400)
        creative = gland.settle(
            Signals(headroom_mb=20000, alert_rate_1h=1, hour=14, creative_mode=True), 400
        )
        assert creative.dopamine > base.dopamine
        assert creative.cortisol <= base.cortisol + 1e-9

    def test_blind_signals_hold_neutral_not_assume_calm(self) -> None:
        # Honestly blind: if a signal is unreadable (None), the gland must NOT
        # fabricate "all calm" — it holds the previous/neutral level for that
        # axis rather than driving it to 0.
        blind = Signals(headroom_mb=None, alert_rate_1h=None, hour=14)
        p = gland.settle(blind, 400, start=Panel.neutral())
        # cortisol/noradrenaline stay at their neutral setpoints, not slammed to 0
        assert p.cortisol == pytest.approx(Regulator.for_hormone("cortisol").setpoint, abs=0.05)


# ───────────────── receptor contract (REAL receptor, REAL panel) ──────────


class _FakeSeat:
    """A stand-in seat with only the fields the receptor reads — derived from
    the real Seat's public attrs (model, persona), NOT from receptor internals,
    so the test and production don't share one mental model."""

    def __init__(self) -> None:
        self.label = "yoda"
        self.model = "council-max-thinking"
        self.persona = "You are Yoda."


class TestReceptorContract:
    def test_neutral_panel_is_byte_identical_to_today(self) -> None:
        # OFF-BY-DEFAULT: a neutral panel must add NOTHING to the payload.
        seat = _FakeSeat()
        delta = receptor.sampling_for(seat, Panel.neutral())
        assert delta == {}, "neutral panel must not touch sampling — off by default"

    def test_absent_gland_is_byte_identical_to_today(self) -> None:
        # No panel at all (gland down / not subscribed) → empty delta.
        seat = _FakeSeat()
        assert receptor.sampling_for(seat, None) == {}

    def test_creative_panel_actually_raises_temperature(self) -> None:
        # CONTRACT not field: feed a REAL high-dopamine/low-cortisol panel through
        # the REAL receptor and assert the EFFECTIVE temperature truly rises and
        # is a value proxyd will forward (0..2 range, the translate.rs key set).
        seat = _FakeSeat()
        creative = Panel(
            dopamine=0.95,
            cortisol=0.05,
            noradrenaline=0.1,
            oxytocin=0.2,
            melatonin=0.1,
            serotonin=0.7,
        )
        delta = receptor.sampling_for(seat, creative)
        assert "temperature" in delta
        assert delta["temperature"] > receptor.BASELINE_TEMPERATURE
        assert 0.0 <= delta["temperature"] <= 2.0  # proxyd-forwardable range

    def test_high_cortisol_panel_lowers_temperature_convergent(self) -> None:
        # Stress = focused/convergent: cortisol must pull temperature DOWN.
        seat = _FakeSeat()
        stressed = Panel(
            dopamine=0.1,
            cortisol=0.95,
            noradrenaline=0.8,
            oxytocin=0.6,
            melatonin=0.1,
            serotonin=0.3,
        )
        delta = receptor.sampling_for(seat, stressed)
        assert delta["temperature"] < receptor.BASELINE_TEMPERATURE
        assert delta["temperature"] >= 0.0

    def test_framing_clause_diverges_under_dopamine_converges_under_cortisol(self) -> None:
        creative = Panel(
            dopamine=0.95,
            cortisol=0.05,
            noradrenaline=0.1,
            oxytocin=0.2,
            melatonin=0.1,
            serotonin=0.7,
        )
        stressed = Panel(
            dopamine=0.1,
            cortisol=0.95,
            noradrenaline=0.8,
            oxytocin=0.6,
            melatonin=0.1,
            serotonin=0.3,
        )
        div = receptor.framing_clause(creative)
        con = receptor.framing_clause(stressed)
        assert div != con
        assert "diverg" in div.lower() or "explore" in div.lower() or "wild" in div.lower()
        assert "converg" in con.lower() or "focus" in con.lower() or "conserv" in con.lower()
        # neutral panel adds no clause at all (off by default)
        assert receptor.framing_clause(Panel.neutral()) == ""

    def test_creative_mode_engages_max_diversity_within_subscription(self) -> None:
        # "neurodiversity paramount" made dynamic — but SUBSCRIPTION-FIRST:
        # the engaged seat set must NEVER include a metered/openrouter seat.
        creative = Panel(
            dopamine=0.95,
            cortisol=0.05,
            noradrenaline=0.1,
            oxytocin=0.2,
            melatonin=0.1,
            serotonin=0.7,
        )
        all_seats = {
            "yoda": "council-max-thinking",  # subscription
            "windu": "council-spacial",  # subscription
            "quigon": "council-code",  # local/free
            "cilghal": "council-mlx",  # local/free
            "fallback_glm": "glm-4.6",  # METERED openrouter
        }
        engaged = receptor.diversity_seats(creative, all_seats, metered={"fallback_glm"})
        assert "fallback_glm" not in engaged, "creative mode must not route to metered"
        # max diversity engages MORE seats than the neutral baseline
        neutral_engaged = receptor.diversity_seats(
            Panel.neutral(), all_seats, metered={"fallback_glm"}
        )
        assert len(engaged) >= len(neutral_engaged)


# ───────────────── real gland→broadcast→receptor loop (no mocks) ──────────


class TestRealLoop:
    def test_gland_to_receptor_round_trip(self) -> None:
        # The REAL loop: real signals → real gland compute → serialize to the
        # broadcast wire shape → real receptor read → effective knob change.
        # No subprocess/network mocked; this is a pure in-process integration.
        sig = Signals(headroom_mb=30000, alert_rate_1h=1, hour=15, creative_mode=True)
        panel = gland.settle(sig, 400)

        wire = panel.to_wire()  # what gets POSTed to chitti / served on /panel
        assert set(HORMONES).issubset(wire.keys())
        assert all(0.0 <= wire[h] <= 1.0 for h in HORMONES)

        # a fresh receptor reading the wire (not the in-memory object) must act
        roundtripped = Panel.from_wire(wire)
        seat = _FakeSeat()
        delta = receptor.sampling_for(seat, roundtripped)
        assert (
            delta.get("temperature", receptor.BASELINE_TEMPERATURE) > receptor.BASELINE_TEMPERATURE
        )

    def test_samskara_record_shape_matches_existing_writers(self) -> None:
        # The timeseries record the gland appends must match the LIVE samskara
        # schema (ts/service/pattern/action/success) so existing readers parse it.
        # Expectation derived from a live row, NOT from gland code.
        live_keys = {"ts", "service", "pattern", "action", "success"}
        sig = Signals(headroom_mb=30000, alert_rate_1h=1, hour=15)
        rec = gland.samskara_record(gland.settle(sig, 400))
        assert live_keys.issubset(rec.keys())
        assert rec["service"] == "endocrine"
        assert isinstance(rec["success"], bool)
        # ISO-8601 Z timestamp like the live rows
        assert rec["ts"].endswith("Z")


# ───────────── bloodstream: the on-disk broadcast surface (real files) ─────


class TestBloodstream:
    """The query/lever file surface — real writes + reads, no mocks."""

    def test_publish_then_read_round_trips_the_panel(self, tmp_path, monkeypatch) -> None:
        from sanctum_cli.endocrine import bloodstream

        monkeypatch.setenv(bloodstream.STATE_DIR_ENV, str(tmp_path))

        sig = Signals(headroom_mb=30000, alert_rate_1h=1, hour=15, creative_mode=True)
        published = gland.settle(sig, 400)
        bloodstream.publish_panel_file(published)

        # a fresh reader (off the file, not the in-memory object) gets it back
        got = bloodstream.read_panel()
        assert got is not None
        for h in HORMONES:
            assert getattr(got, h) == pytest.approx(getattr(published, h), abs=1e-3)

    def test_absent_bloodstream_reads_none_off_by_default(self, tmp_path, monkeypatch) -> None:
        from sanctum_cli.endocrine import bloodstream

        monkeypatch.setenv(bloodstream.STATE_DIR_ENV, str(tmp_path / "nonexistent"))
        # nothing published yet → None → the receptor's off-by-default path
        assert bloodstream.read_panel() is None

    def test_garbage_panel_file_reads_none_never_fabricates(self, tmp_path, monkeypatch) -> None:
        from sanctum_cli.endocrine import bloodstream

        monkeypatch.setenv(bloodstream.STATE_DIR_ENV, str(tmp_path))
        bloodstream.panel_path().parent.mkdir(parents=True, exist_ok=True)
        bloodstream.panel_path().write_text("{not json at all")
        assert bloodstream.read_panel() is None  # garbage → None, not a guess

    def test_creative_lever_round_trips_and_ttl_expires(self, tmp_path, monkeypatch) -> None:
        from sanctum_cli.endocrine import bloodstream

        monkeypatch.setenv(bloodstream.STATE_DIR_ENV, str(tmp_path))

        assert bloodstream.read_creative_mode() is False  # default resting
        bloodstream.set_creative_mode(True)
        assert bloodstream.read_creative_mode() is True
        bloodstream.set_creative_mode(False)
        assert bloodstream.read_creative_mode() is False
        # an expired TTL dose lapses back on its own (can't get stuck hot)
        bloodstream.set_creative_mode(True, ttl_seconds=-1)  # already expired
        assert bloodstream.read_creative_mode() is False


# ───────────── gland daemon: REAL signal reading + one real tick ───────────


class TestGlandDaemon:
    """The live organ wrapper. The network reads are stubbed at the SEAM the
    daemon owns (read_memory_headroom_mb / read_alert_rate_1h) using the chitti
    /fluid + force-flow shapes observed on the LIVE box (a different source than
    the daemon code), then the rest of the tick runs for real — the file write,
    the regulator step, the checkpoint resume — nothing else mocked."""

    def test_tick_reads_signals_steps_and_publishes_real_file(self, tmp_path, monkeypatch) -> None:
        from sanctum_cli.endocrine import bloodstream, gland_daemon

        monkeypatch.setenv(bloodstream.STATE_DIR_ENV, str(tmp_path))

        # live /fluid shape (2026-06-15): memory_available_gb under pressure.
        monkeypatch.setattr(gland_daemon, "read_memory_headroom_mb", lambda **k: 1000)
        monkeypatch.setattr(gland_daemon, "read_alert_rate_1h", lambda **k: 0)
        bloodstream.set_creative_mode(False)

        # cold start: neutral checkpoint; chitti broadcast is best-effort → skip
        monkeypatch.setattr(bloodstream, "broadcast_to_chitti", lambda *a, **k: False)
        first = gland_daemon.tick()
        # 1000 MB headroom is castellan-CRITICAL → cortisol must rise off setpoint
        neutral_cort = Regulator.for_hormone("cortisol").setpoint
        assert first.cortisol > neutral_cort
        # and it was actually published to the file (a fresh read sees it)
        on_disk = bloodstream.read_panel()
        assert on_disk is not None
        assert on_disk.cortisol == pytest.approx(first.cortisol, abs=1e-3)

    def test_tick_resumes_from_checkpoint_not_neutral(self, tmp_path, monkeypatch) -> None:
        from sanctum_cli.endocrine import bloodstream, gland_daemon

        monkeypatch.setenv(bloodstream.STATE_DIR_ENV, str(tmp_path))
        monkeypatch.setattr(gland_daemon, "read_memory_headroom_mb", lambda **k: 1000)
        monkeypatch.setattr(gland_daemon, "read_alert_rate_1h", lambda **k: 0)
        monkeypatch.setattr(bloodstream, "broadcast_to_chitti", lambda *a, **k: False)
        bloodstream.set_creative_mode(False)

        # SLOW modulation: one tick must NOT jump cortisol all the way to the
        # tier target — it lags. Two ticks must move it further than one (the
        # leaky integrator climbing), proving step-once-per-tick, not settle.
        c1 = gland_daemon.tick().cortisol
        c2 = gland_daemon.tick().cortisol
        assert c2 > c1, "second tick must continue climbing from the checkpoint"
        settled = gland.settle(
            Signals(headroom_mb=1000, alert_rate_1h=0, hour=__import__("time").localtime().tm_hour)
        ).cortisol
        assert c1 < settled, "one tick must lag the fixed point (slow, not reflexive)"

    def test_blind_reads_hold_neutral_not_fabricate_calm(self, tmp_path, monkeypatch) -> None:
        from sanctum_cli.endocrine import bloodstream, gland_daemon

        monkeypatch.setenv(bloodstream.STATE_DIR_ENV, str(tmp_path))
        # both reads blind (None) — the daemon must NOT invent "all calm"
        monkeypatch.setattr(gland_daemon, "read_memory_headroom_mb", lambda **k: None)
        monkeypatch.setattr(gland_daemon, "read_alert_rate_1h", lambda **k: None)
        monkeypatch.setattr(bloodstream, "broadcast_to_chitti", lambda *a, **k: False)
        bloodstream.set_creative_mode(False)
        p = gland_daemon.tick()
        # cortisol stays near setpoint (held), not slammed to 0 by a fake calm
        assert p.cortisol == pytest.approx(Regulator.for_hormone("cortisol").setpoint, abs=0.05)


# ───────── council seat path: the receptor REALLY changes a seat's knobs ───
# Contracts-at-the-Boundary: test the produced REQUEST BODY (the artifact that
# crosses to proxyd), via the REAL build_completion_payload — not a mock, not a
# field assertion. Expectation derived from the council Seat's public shape, not
# from receptor internals.


class TestCouncilSeatReceptorContract:
    def _seat(self):
        from sanctum_cli.commands.council import SEATS

        return SEATS["yoda"]

    def test_unsubscribed_payload_is_byte_identical_to_today(self, tmp_path, monkeypatch) -> None:
        from sanctum_cli.commands import council
        from sanctum_cli.endocrine import bloodstream

        monkeypatch.setenv(bloodstream.STATE_DIR_ENV, str(tmp_path))
        monkeypatch.delenv(council.ENDOCRINE_ENV, raising=False)  # NOT subscribed
        # even with a hot panel published, an unsubscribed seat ignores it
        bloodstream.publish_panel_file(
            Panel(
                dopamine=0.95,
                cortisol=0.05,
                noradrenaline=0.1,
                oxytocin=0.2,
                melatonin=0.1,
                serotonin=0.7,
            )
        )
        seat = self._seat()
        body = council.build_completion_payload(
            seat, [{"role": "user", "content": "hi"}], system=seat.persona
        )
        assert "temperature" not in body and "top_p" not in body
        assert body["system"] == seat.persona  # no framing clause appended

    def test_subscribed_creative_panel_raises_real_payload_temperature(
        self, tmp_path, monkeypatch
    ) -> None:
        from sanctum_cli.commands import council
        from sanctum_cli.endocrine import bloodstream

        monkeypatch.setenv(bloodstream.STATE_DIR_ENV, str(tmp_path))
        monkeypatch.setenv(council.ENDOCRINE_ENV, "1")  # seat opts in
        # publish a REAL high-dopamine/low-cortisol panel to the bloodstream file
        bloodstream.publish_panel_file(
            Panel(
                dopamine=0.95,
                cortisol=0.05,
                noradrenaline=0.1,
                oxytocin=0.2,
                melatonin=0.1,
                serotonin=0.7,
            )
        )
        seat = self._seat()
        body = council.build_completion_payload(
            seat, [{"role": "user", "content": "hi"}], system=seat.persona
        )
        # the ACTUAL request body proxyd would receive now carries a hotter temp
        assert "temperature" in body
        assert body["temperature"] > council.receptor.BASELINE_TEMPERATURE
        assert 0.0 <= body["temperature"] <= 2.0  # proxyd-forwardable
        # and the system prompt was tilted divergent (additive, persona intact)
        assert body["system"].startswith(seat.persona)
        assert body["system"] != seat.persona
        low = body["system"].lower()
        assert "diverg" in low or "explore" in low or "wild" in low

    def test_subscribed_stress_panel_lowers_real_payload_temperature(
        self, tmp_path, monkeypatch
    ) -> None:
        from sanctum_cli.commands import council
        from sanctum_cli.endocrine import bloodstream

        monkeypatch.setenv(bloodstream.STATE_DIR_ENV, str(tmp_path))
        monkeypatch.setenv(council.ENDOCRINE_ENV, "1")
        bloodstream.publish_panel_file(
            Panel(
                dopamine=0.1,
                cortisol=0.95,
                noradrenaline=0.8,
                oxytocin=0.6,
                melatonin=0.1,
                serotonin=0.3,
            )
        )
        seat = self._seat()
        body = council.build_completion_payload(
            seat, [{"role": "user", "content": "hi"}], system=seat.persona
        )
        assert body["temperature"] < council.receptor.BASELINE_TEMPERATURE
        assert "converg" in body["system"].lower() or "focus" in body["system"].lower()

    def test_subscribed_neutral_panel_still_off_by_default(self, tmp_path, monkeypatch) -> None:
        # Subscribed but the gland is at homeostasis → STILL no change (the
        # off-by-default property is the NEUTRAL panel, not just the unsubscribed
        # case). This is the "no behavior change until it departs" guarantee.
        from sanctum_cli.commands import council
        from sanctum_cli.endocrine import bloodstream

        monkeypatch.setenv(bloodstream.STATE_DIR_ENV, str(tmp_path))
        monkeypatch.setenv(council.ENDOCRINE_ENV, "1")
        bloodstream.publish_panel_file(Panel.neutral())
        seat = self._seat()
        body = council.build_completion_payload(
            seat, [{"role": "user", "content": "hi"}], system=seat.persona
        )
        assert "temperature" not in body
        assert body["system"] == seat.persona
