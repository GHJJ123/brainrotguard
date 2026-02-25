"""Tests for bot/callback_router.py — declarative callback dispatch."""

import pytest
from bot.callback_router import CallbackRoute, match_route, _build_args


# -- Route fixtures ----------------------------------------------------------

def _routes():
    """Build a representative route table for testing."""
    AB = frozenset({"allowed", "blocked"})
    return [
        CallbackRoute("child_sel", "_cb_child_select", min_parts=2, answer="", pass_update=True),
        CallbackRoute("autoapprove", "_cb_auto_approve", min_parts=3, answer="Auto-approved!"),
        CallbackRoute("approved_page", "_cb_approved_page", min_parts=3, int_parts=frozenset({2})),
        CallbackRoute("logs_page", "_cb_logs_page", min_parts=4, int_parts=frozenset({2, 3})),
        CallbackRoute("chan_page", "_cb_channel_page", min_parts=4,
                       constraints={2: AB}, int_parts=frozenset({3})),
        CallbackRoute("chan_filter", "_cb_channel_filter", min_parts=3,
                       constraints={2: AB}),
        CallbackRoute("setup_sched_start", "_cb_setup_sched_start", min_parts=2, rejoin_from=1),
        CallbackRoute("setup_daystart", "_cb_setup_daystart", min_parts=3, rejoin_from=2),
        CallbackRoute("setup_sched_done", "_cb_setup_sched_done", min_parts=1),
        CallbackRoute("unallow", "_cb_channel_remove", min_parts=3, rejoin_from=2),
        CallbackRoute("starter_import", "_cb_starter_import", min_parts=3, int_parts=frozenset({2})),
    ]


# -- CallbackRoute defaults ---------------------------------------------------

class TestCallbackRouteDefaults:
    def test_max_parts_defaults_to_min(self):
        r = CallbackRoute("test", "_handler", min_parts=3)
        assert r.max_parts == 3

    def test_max_parts_explicit(self):
        r = CallbackRoute("test", "_handler", min_parts=2, max_parts=5)
        assert r.max_parts == 5

    def test_frozen(self):
        r = CallbackRoute("test", "_handler")
        with pytest.raises(AttributeError):
            r.prefix = "other"


# -- match_route basic matching -----------------------------------------------

class TestMatchRoute:
    def test_simple_match(self):
        routes = _routes()
        result = match_route(routes, ["child_sel", "profile1"])
        assert result is not None
        route, args = result
        assert route.handler == "_cb_child_select"
        assert args == ["profile1"]

    def test_no_match_wrong_prefix(self):
        routes = _routes()
        assert match_route(routes, ["unknown", "foo"]) is None

    def test_no_match_too_few_parts(self):
        routes = _routes()
        # autoapprove needs 3 parts
        assert match_route(routes, ["autoapprove", "pid"]) is None

    def test_no_match_too_many_parts(self):
        routes = _routes()
        # child_sel expects exactly 2 parts
        assert match_route(routes, ["child_sel", "pid", "extra"]) is None

    def test_three_part_match(self):
        routes = _routes()
        result = match_route(routes, ["autoapprove", "pid", "vid"])
        assert result is not None
        route, args = result
        assert route.handler == "_cb_auto_approve"
        assert args == ["pid", "vid"]

    def test_single_part_match(self):
        routes = _routes()
        result = match_route(routes, ["setup_sched_done"])
        assert result is not None
        route, args = result
        assert route.handler == "_cb_setup_sched_done"
        assert args == []


# -- Int conversion -----------------------------------------------------------

class TestIntConversion:
    def test_int_part_converted(self):
        routes = _routes()
        result = match_route(routes, ["approved_page", "pid", "5"])
        assert result is not None
        _, args = result
        assert args == ["pid", 5]

    def test_multiple_int_parts(self):
        routes = _routes()
        result = match_route(routes, ["logs_page", "pid", "7", "2"])
        assert result is not None
        _, args = result
        assert args == ["pid", 7, 2]

    def test_int_conversion_failure_skips_route(self):
        routes = _routes()
        # "abc" can't be converted to int
        assert match_route(routes, ["approved_page", "pid", "abc"]) is None

    def test_int_with_constraint(self):
        routes = _routes()
        result = match_route(routes, ["chan_page", "pid", "allowed", "3"])
        assert result is not None
        _, args = result
        assert args == ["pid", "allowed", 3]

    def test_starter_import_int(self):
        routes = _routes()
        result = match_route(routes, ["starter_import", "pid", "0"])
        assert result is not None
        _, args = result
        assert args == ["pid", 0]


# -- Value constraints --------------------------------------------------------

class TestConstraints:
    def test_constraint_satisfied(self):
        routes = _routes()
        result = match_route(routes, ["chan_filter", "pid", "allowed"])
        assert result is not None
        _, args = result
        assert args == ["pid", "allowed"]

    def test_constraint_blocked_value(self):
        routes = _routes()
        result = match_route(routes, ["chan_filter", "pid", "blocked"])
        assert result is not None

    def test_constraint_rejected(self):
        routes = _routes()
        # "pending" is not in {"allowed", "blocked"}
        assert match_route(routes, ["chan_filter", "pid", "pending"]) is None

    def test_chan_page_constraint_rejected(self):
        routes = _routes()
        assert match_route(routes, ["chan_page", "pid", "pending", "0"]) is None


# -- Colon rejoin -------------------------------------------------------------

class TestRejoin:
    def test_rejoin_time_value(self):
        routes = _routes()
        # setup_sched_start:08:30 → parts = ["setup_sched_start", "08", "30"]
        result = match_route(routes, ["setup_sched_start", "08", "30"])
        assert result is not None
        _, args = result
        # rejoin_from=1 → parts[1:] joined = "08:30"
        assert args == ["08:30"]

    def test_rejoin_simple_value(self):
        routes = _routes()
        # setup_sched_start:custom → parts = ["setup_sched_start", "custom"]
        result = match_route(routes, ["setup_sched_start", "custom"])
        assert result is not None
        _, args = result
        assert args == ["custom"]

    def test_rejoin_daystart_with_time(self):
        routes = _routes()
        # setup_daystart:mon:08:30 → day=mon, value=08:30
        result = match_route(routes, ["setup_daystart", "mon", "08", "30"])
        assert result is not None
        _, args = result
        assert args == ["mon", "08:30"]

    def test_rejoin_channel_name_with_colons(self):
        routes = _routes()
        # unallow:pid:Some:Channel:Name → action prefix, pid, ch_name="Some:Channel:Name"
        result = match_route(routes, ["unallow", "pid", "Some", "Channel", "Name"])
        assert result is not None
        _, args = result
        assert args == ["pid", "Some:Channel:Name"]

    def test_rejoin_simple_channel_name(self):
        routes = _routes()
        result = match_route(routes, ["unallow", "pid", "SimpleChannel"])
        assert result is not None
        _, args = result
        assert args == ["pid", "SimpleChannel"]


# -- _build_args edge cases ---------------------------------------------------

class TestBuildArgs:
    def test_no_rejoin_no_int(self):
        route = CallbackRoute("test", "_handler", min_parts=3)
        args = _build_args(route, ["test", "a", "b"])
        assert args == ["a", "b"]

    def test_empty_parts_after_prefix(self):
        route = CallbackRoute("test", "_handler", min_parts=1)
        args = _build_args(route, ["test"])
        assert args == []

    def test_int_conversion_failure_returns_none(self):
        route = CallbackRoute("test", "_handler", min_parts=2, int_parts=frozenset({1}))
        assert _build_args(route, ["test", "not_a_number"]) is None


# -- Route table used by BrainRotGuardBot ------------------------------------

class TestBotRouteTable:
    """Verify the actual route table from the bot class has no duplicate prefixes
    and all handler names are well-formed."""

    @pytest.fixture
    def bot_routes(self):
        from bot.telegram_bot import BrainRotGuardBot
        return BrainRotGuardBot._CALLBACK_ROUTES

    def test_all_routes_have_handler(self, bot_routes):
        for route in bot_routes:
            assert route.handler.startswith("_cb_")

    def test_no_duplicate_prefixes_unless_intentional(self, bot_routes):
        # unallow and unblock share _cb_channel_remove — that's intentional
        prefixes = [r.prefix for r in bot_routes]
        # Check no accidental duplicates (same prefix twice)
        seen = set()
        for p in prefixes:
            if p in seen:
                pytest.fail(f"Duplicate prefix in route table: {p}")
            seen.add(p)

    def test_all_handlers_exist_on_bot(self, bot_routes):
        from bot.telegram_bot import BrainRotGuardBot
        for route in bot_routes:
            assert hasattr(BrainRotGuardBot, route.handler), \
                f"Handler {route.handler} not found on BrainRotGuardBot"

    def test_route_count(self, bot_routes):
        # Sanity check: we should have a reasonable number of routes
        assert len(bot_routes) >= 25
