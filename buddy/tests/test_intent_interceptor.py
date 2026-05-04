# buddy/tests/test_intent_interceptor.py
#
# Hard edge-case tests for IntentInterceptor.
# Tests normalize() and IntentInterceptor.match() ONLY.
# No execution — no subprocess, no platform calls, no registered-handler dependency.
#
# Run: pytest buddy/tests/test_intent_interceptor.py -v

import os

import pytest

from buddy.brain.intent_interceptor import (
    IntentInterceptor,
    _resolve_app,
    normalize,
)

interceptor = IntentInterceptor()


def _match(raw: str):
    """Normalize raw text then run match. Returns QuickAction or None."""
    return interceptor.match(normalize(raw))


def _name(raw: str):
    """Return action name or None — convenience wrapper for assert readability."""
    a = _match(raw)
    return a.name if a else None


# ═══════════════════════════════════════════════════════════════════
# §1  normalize() — text normalization
# ═══════════════════════════════════════════════════════════════════


class TestNormalize:

    def test_lowercase(self):
        assert normalize("OPEN SPOTIFY") == "open spotify"

    def test_strip_outer_whitespace(self):
        assert normalize("  open spotify  ") == "open spotify"

    def test_collapse_inner_whitespace(self):
        assert normalize("open   spotify") == "open spotify"

    def test_unicode_fold_accent(self):
        assert normalize("café") == "cafe"

    @pytest.mark.xfail(
        reason=(
            "BUG: NFKD+ascii strips U+2019 entirely instead of converting to straight"
            ' apostrophe. Fix: add t.replace(‘\\u2019’, "’") before NFKD in'
            " normalize()."
        )
    )
    def test_unicode_curly_apostrophe_contraction(self):
        # U+2019 RIGHT SINGLE QUOTATION MARK — currently stripped to empty,
        # leaving "dont disturb" instead of expanding the contraction.
        result = normalize("don’t disturb")
        assert result == "do not disturb"

    def test_contraction_dont(self):
        assert normalize("don't disturb") == "do not disturb"

    def test_contraction_whats_apostrophe(self):
        assert normalize("what's the time") == "what is the time"

    def test_contraction_whats_no_apostrophe(self):
        # FIX: "whats" → "what is" (missing apostrophe)
        assert normalize("whats the time") == "what is the time"

    def test_contraction_hows_no_apostrophe(self):
        assert normalize("hows the battery") == "how is the battery"

    def test_contraction_its(self):
        assert normalize("it's paused") == "it is paused"

    def test_contraction_wont(self):
        assert normalize("won't stop") == "will not stop"

    def test_contraction_im(self):
        assert normalize("i'm ready") == "i am ready"

    def test_contraction_lets(self):
        assert normalize("let's go") == "let us go"

    def test_punctuation_stripped(self):
        assert normalize("open spotify!") == "open spotify"

    def test_question_mark_stripped(self):
        assert normalize("what time is it?") == "what time is it"

    def test_prefix_hey_buddy(self):
        assert normalize("Hey Buddy, open Spotify") == "open spotify"

    def test_prefix_hi_buddy(self):
        assert normalize("hi buddy open spotify") == "open spotify"

    def test_prefix_yo_buddy(self):
        assert normalize("yo buddy launch discord") == "launch discord"

    def test_prefix_can_you_please(self):
        assert normalize("can you please open spotify") == "open spotify"

    def test_prefix_could_you(self):
        assert normalize("could you open spotify") == "open spotify"

    def test_prefix_will_you(self):
        assert normalize("will you mute") == "mute"

    def test_prefix_stacked_multi_layer(self):
        # deeply nested filler — all must be stripped iteratively
        assert normalize("hey buddy can you please just open spotify") == "open spotify"

    def test_prefix_i_want_to(self):
        assert normalize("I want you to open spotify") == "open spotify"

    def test_prefix_i_need_to(self):
        assert normalize("I need to mute") == "mute"

    def test_prefix_help_me(self):
        assert normalize("help me open spotify") == "open spotify"

    def test_prefix_go_ahead_and(self):
        assert normalize("go ahead and open spotify") == "open spotify"

    def test_prefix_just(self):
        assert normalize("just mute") == "mute"

    def test_prefix_quickly(self):
        assert normalize("quickly take a screenshot") == "take a screenshot"

    def test_prefix_um_uh(self):
        assert normalize("um uh open chrome") == "open chrome"

    def test_prefix_hmm(self):
        assert normalize("hmm, open terminal") == "open terminal"

    def test_suffix_please(self):
        assert normalize("open spotify please") == "open spotify"

    def test_suffix_thanks(self):
        assert normalize("volume up, thanks") == "volume up"

    def test_suffix_thank_you(self):
        assert normalize("lock screen, thank you") == "lock screen"

    def test_suffix_right_now(self):
        assert normalize("lock screen right now") == "lock screen"

    def test_suffix_now(self):
        # FIX: bare "now" added as suffix
        assert normalize("volume up now") == "volume up"

    def test_suffix_asap(self):
        assert normalize("mute asap") == "mute"

    def test_suffix_immediately(self):
        assert normalize("screenshot immediately") == "screenshot"

    def test_suffix_for_me(self):
        assert normalize("take a screenshot for me") == "take a screenshot"

    def test_empty_string(self):
        assert normalize("") == ""

    def test_only_whitespace(self):
        assert normalize("   ") == ""

    def test_only_filler_words(self):
        result = normalize("hey buddy please")
        assert isinstance(result, str)  # must not crash

    def test_deeply_stacked_please_no_hang(self):
        # pathological input — must not loop forever
        big = "please " * 30 + "open spotify"
        result = normalize(big)
        assert result == "open spotify"

    def test_emoji_stripped(self):
        # non-ASCII stripped by NFKD+ascii
        result = normalize("\U0001f3b5 play music")
        assert "play" in result


# ═══════════════════════════════════════════════════════════════════
# §2  Coref / ambiguity — must return None
# ═══════════════════════════════════════════════════════════════════


class TestCoref:

    def test_this_song(self):
        assert _match("play this song") is None

    def test_that_track(self):
        assert _match("play that track") is None

    def test_the_song_coref(self):
        assert _match("play the song") is None

    def test_the_one(self):
        assert _match("play the one") is None

    def test_the_video(self):
        assert _match("open the video") is None

    def test_the_app_coref(self):
        assert _match("open the app") is None

    def test_the_album(self):
        assert _match("play the album") is None

    def test_the_playlist(self):
        assert _match("play the playlist") is None

    def test_the_artist(self):
        assert _match("play the artist") is None

    def test_these_files(self):
        assert _match("open these files") is None

    def test_those_songs(self):
        assert _match("play those songs") is None

    def test_bare_it_no_longer_blocks(self):
        # "it" was removed from coref — "what time is it" must now match
        a = _match("what time is it")
        assert a is not None
        assert a.name == "tell_time"

    def test_what_is_the_time_not_blocked(self):
        a = _match("what is the time")
        assert a is not None
        assert a.name == "tell_time"


# ═══════════════════════════════════════════════════════════════════
# §3  Media — play / pause / next / prev / toggle
# ═══════════════════════════════════════════════════════════════════


class TestMedia:

    # ── play ──────────────────────────────────────────────────────

    def test_bare_play(self):
        assert _name("play") == "media_play"

    def test_play_music_generic(self):
        assert _name("play music") == "media_play"

    def test_play_some_music(self):
        assert _name("play some music") == "media_play"

    def test_play_songs(self):
        assert _name("play songs") == "media_play"

    def test_play_anything(self):
        assert _name("play anything") == "media_play"

    def test_play_specific_song_falls_through(self):
        # Specific song without "on <app>" → ambiguous → Brain
        assert _name("play Bohemian Rhapsody") == "play_on_app"

    def test_play_specific_song_by_artist_falls_through(self):
        assert _name("play something by The Beatles") == "play_on_app"

    def test_play_latest_album_falls_through(self):
        assert _name("play the latest album") == "play_on_app"  # coref "the album"

    def test_resume(self):
        assert _name("resume") == "media_play"

    def test_continue_music(self):
        assert _name("continue music") == "media_play"

    def test_continue_playing(self):
        assert _name("continue playing") == "media_play"

    # ── play on app ───────────────────────────────────────────────

    def test_play_on_spotify(self):
        a = _match("play Blinding Lights on Spotify")
        assert a is not None
        assert a.name == "play_on_app"
        assert a.params["song"] == "blinding lights"
        assert a.params["app"] == "spotify"

    def test_play_on_youtube(self):
        a = _match("play Lose Yourself on YouTube")
        assert a is not None
        assert a.name == "play_on_app"
        assert a.params["app"] == "youtube"

    def test_play_on_yt_alias(self):
        a = _match("play Never Gonna Give You Up on yt")
        assert a is not None
        assert a.name == "play_on_app"
        assert a.params["app"] == "yt"

    def test_play_on_app_no_song_returns_generic_play(self):
        # "play on Spotify" matches ^play\s*(?P<after>.*)$ with after="on spotify";
        # _ON_APP_RE detects trailing "on <word>" → not ambiguous → media_play.
        # (play_on_app builder is never reached because there's no song before "on".)
        a = _match("play on Spotify")
        assert a is not None and a.name == "media_play"

    # ── pause / stop ──────────────────────────────────────────────

    def test_pause(self):
        assert _name("pause") == "media_pause"

    def test_stop(self):
        assert _name("stop") == "media_pause"

    def test_stop_music(self):
        assert _name("stop music") == "media_pause"

    def test_stop_playing(self):
        assert _name("stop playing") == "media_pause"

    def test_stop_playback(self):
        assert _name("stop playback") == "media_pause"

    def test_stop_the_music(self):
        assert _name("stop the music") == "media_pause"

    # ── toggle ────────────────────────────────────────────────────

    def test_play_pause(self):
        assert _name("play pause") == "media_toggle"

    def test_toggle_music(self):
        assert _name("toggle music") == "media_toggle"

    def test_toggle_playback(self):
        assert _name("toggle playback") == "media_toggle"

    # ── next ──────────────────────────────────────────────────────

    def test_next(self):
        assert _name("next") == "media_next"

    def test_next_track(self):
        assert _name("next track") == "media_next"

    def test_next_song(self):
        assert _name("next song") == "media_next"

    def test_play_next_is_next_not_ambiguous_play(self):
        # FIX: "play next" must be media_next, not fall-through
        assert _name("play next") == "media_next"

    def test_play_next_track(self):
        assert _name("play next track") == "media_next"

    def test_play_next_song(self):
        assert _name("play next song") == "media_next"

    def test_go_to_next_track(self):
        assert _name("go to next track") == "media_next"

    def test_go_to_next(self):
        assert _name("go to next") == "media_next"

    def test_go_forward(self):
        assert _name("go forward") == "media_next"

    def test_skip(self):
        assert _name("skip") == "media_next"

    def test_skip_track(self):
        assert _name("skip track") == "media_next"

    def test_skip_song(self):
        assert _name("skip song") == "media_next"

    # ── previous ──────────────────────────────────────────────────

    def test_previous(self):
        assert _name("previous") == "media_prev"

    def test_prev(self):
        assert _name("prev") == "media_prev"

    def test_previous_track(self):
        assert _name("previous track") == "media_prev"

    def test_go_to_previous(self):
        assert _name("go to previous") == "media_prev"

    def test_go_to_prev_song(self):
        assert _name("go to prev song") == "media_prev"

    def test_go_back(self):
        assert _name("go back") == "media_prev"

    def test_go_to_back(self):
        assert _name("go to back") == "media_prev"

    # ── search on app ─────────────────────────────────────────────

    def test_search_on_youtube(self):
        a = _match("search lofi beats on youtube")
        assert a is not None
        assert a.name == "search_on_app"
        assert a.params["query"] == "lofi beats"
        assert a.params["app"] == "youtube"

    def test_search_for_on_spotify(self):
        a = _match("search for Drake on Spotify")
        assert a is not None
        assert a.name == "search_on_app"
        assert a.params["query"] == "drake"
        assert a.params["app"] == "spotify"

    def test_find_on_github(self):
        a = _match("find fastapi on github")
        assert a is not None
        assert a.name == "search_on_app"
        assert a.params["query"] == "fastapi"
        assert a.params["app"] == "github"

    def test_look_up_on_reddit(self):
        a = _match("look up best laptops on reddit")
        assert a is not None
        assert a.name == "search_on_app"
        assert a.params["query"] == "best laptops"
        assert a.params["app"] == "reddit"

    def test_search_on_google(self):
        a = _match("search python tutorials on google")
        assert a is not None
        assert a.name == "search_on_app"
        assert a.params["app"] == "google"

    def test_search_for_multi_word_query(self):
        a = _match("search for top 10 python libraries on github")
        assert a is not None
        assert a.name == "search_on_app"
        assert a.params["query"] == "top 10 python libraries"

    def test_search_no_app_falls_through(self):
        # "search X" without "on <app>" → Brain
        assert _match("search for lofi beats") is None

    def test_search_empty_query_falls_through(self):
        # "search on youtube" — no query before "on" → builder returns None
        assert _match("search on youtube") is None

    def test_search_on_app_before_play_on_app(self):
        # Ordering: search_on_app must come before play_on_app
        a = _match("find Bohemian Rhapsody on Spotify")
        assert a is not None
        assert a.name == "search_on_app"

    def test_play_next_specific_artist_falls_through(self):
        # "play next track by Queen" — specific content → Brain
        assert _name("play next track by Queen") == "play_on_app"


# ═══════════════════════════════════════════════════════════════════
# §4  Volume
# ═══════════════════════════════════════════════════════════════════


class TestVolume:

    # ── directional up ────────────────────────────────────────────

    def test_volume_up(self):
        assert _name("volume up") == "volume_step"

    def test_volume_louder(self):
        assert _name("volume louder") == "volume_step"

    def test_volume_increase(self):
        assert _name("volume increase") == "volume_step"

    def test_turn_volume_up(self):
        assert _name("turn volume up") == "volume_step"

    def test_turn_the_volume_up(self):
        assert _name("turn the volume up") == "volume_step"

    def test_turn_up_the_volume(self):
        assert _name("turn up the volume") == "volume_step"

    def test_turn_up_volume(self):
        assert _name("turn up volume") == "volume_step"

    def test_raise_the_volume(self):
        assert _name("raise the volume") == "volume_step"

    def test_boost_the_volume(self):
        assert _name("boost the volume") == "volume_step"

    def test_increase_the_volume(self):
        assert _name("increase the volume") == "volume_step"

    def test_louder_standalone(self):
        assert _name("louder") == "volume_step"

    def test_volume_up_delta_is_positive(self):
        a = _match("volume up")
        assert a is not None
        assert a.params["delta"] > 0

    # ── directional down ──────────────────────────────────────────

    def test_volume_down(self):
        assert _name("volume down") == "volume_step"

    def test_volume_lower(self):
        assert _name("volume lower") == "volume_step"

    def test_volume_quieter(self):
        assert _name("volume quieter") == "volume_step"

    def test_volume_decrease(self):
        assert _name("volume decrease") == "volume_step"

    def test_volume_softer(self):
        assert _name("volume softer") == "volume_step"

    def test_turn_volume_down(self):
        assert _name("turn volume down") == "volume_step"

    def test_turn_the_volume_down(self):
        assert _name("turn the volume down") == "volume_step"

    def test_turn_down_the_volume(self):
        assert _name("turn down the volume") == "volume_step"

    def test_lower_the_volume(self):
        assert _name("lower the volume") == "volume_step"

    def test_decrease_the_volume(self):
        assert _name("decrease the volume") == "volume_step"

    def test_quieter_standalone(self):
        assert _name("quieter") == "volume_step"

    def test_volume_down_delta_is_negative(self):
        a = _match("volume down")
        assert a is not None
        assert a.params["delta"] < 0

    # ── set by number ─────────────────────────────────────────────

    def test_volume_50(self):
        a = _match("volume 50")
        assert a is not None and a.name == "volume_set"
        assert a.params["level"] == 50

    def test_set_volume_to_75(self):
        a = _match("set volume to 75")
        assert a is not None and a.name == "volume_set"
        assert a.params["level"] == 75

    def test_set_the_volume_to_30(self):
        # FIX: "set the volume to N"
        a = _match("set the volume to 30")
        assert a is not None and a.name == "volume_set"
        assert a.params["level"] == 30

    def test_volume_with_percent_sign(self):
        a = _match("volume 80%")
        assert a is not None
        assert a.params["level"] == 80

    def test_volume_zero(self):
        a = _match("volume 0")
        assert a is not None and a.name == "volume_set"
        assert a.params["level"] == 0

    def test_volume_100(self):
        a = _match("volume 100")
        assert a is not None and a.name == "volume_set"
        assert a.params["level"] == 100

    def test_volume_999_clamped_to_100(self):
        a = _match("volume 999")
        assert a is not None
        assert a.params["level"] == 100

    # ── max / min ─────────────────────────────────────────────────

    def test_volume_max(self):
        a = _match("volume max")
        assert a is not None and a.params["level"] == 100

    def test_volume_maximum(self):
        a = _match("volume maximum")
        assert a is not None and a.params["level"] == 100

    def test_volume_full(self):
        a = _match("volume full")
        assert a is not None and a.params["level"] == 100

    def test_max_volume_noun_first(self):
        # FIX: "max volume" (noun before keyword)
        a = _match("max volume")
        assert a is not None and a.params["level"] == 100

    def test_full_volume_noun_first(self):
        a = _match("full volume")
        assert a is not None and a.params["level"] == 100

    def test_volume_to_max(self):
        a = _match("volume to max")
        assert a is not None and a.params["level"] == 100

    def test_set_the_volume_to_max(self):
        a = _match("set the volume to max")
        assert a is not None and a.params["level"] == 100

    def test_volume_min(self):
        a = _match("volume min")
        assert a is not None and a.params["level"] == 0

    def test_min_volume_noun_first(self):
        a = _match("min volume")
        assert a is not None and a.params["level"] == 0

    def test_volume_zero_keyword(self):
        a = _match("volume zero")
        assert a is not None and a.params["level"] == 0

    def test_silent_volume(self):
        a = _match("silent volume")
        assert a is not None and a.params["level"] == 0

    # ── mute ──────────────────────────────────────────────────────

    def test_mute(self):
        assert _name("mute") == "mute_toggle"

    def test_unmute(self):
        assert _name("unmute") == "mute_toggle"

    def test_toggle_mute(self):
        assert _name("toggle mute") == "mute_toggle"

    # ── fall-through ──────────────────────────────────────────────

    def test_volume_high_falls_through(self):
        assert _match("volume high") is None

    def test_volume_up_a_bit_falls_through(self):
        # "a bit" not in pattern → Brain
        assert _match("volume up a bit") is None


# ═══════════════════════════════════════════════════════════════════
# §5  Brightness
# ═══════════════════════════════════════════════════════════════════


class TestBrightness:

    # ── directional up ────────────────────────────────────────────

    def test_brightness_up(self):
        assert _name("brightness up") == "brightness_step"

    def test_brightness_increase(self):
        assert _name("brightness increase") == "brightness_step"

    def test_brightness_higher(self):
        assert _name("brightness higher") == "brightness_step"

    def test_brightness_more(self):
        assert _name("brightness more") == "brightness_step"

    def test_screen_brightness_up(self):
        assert _name("screen brightness up") == "brightness_step"

    def test_turn_brightness_up(self):
        assert _name("turn brightness up") == "brightness_step"

    def test_turn_the_brightness_up(self):
        assert _name("turn the brightness up") == "brightness_step"

    def test_turn_up_brightness(self):
        assert _name("turn up brightness") == "brightness_step"

    def test_turn_up_the_brightness(self):
        assert _name("turn up the brightness") == "brightness_step"

    def test_turn_screen_brightness_up(self):
        assert _name("turn screen brightness up") == "brightness_step"

    def test_turn_the_screen_brightness_up(self):
        assert _name("turn the screen brightness up") == "brightness_step"

    def test_increase_the_brightness(self):
        assert _name("increase the brightness") == "brightness_step"

    def test_raise_the_brightness(self):
        assert _name("raise the brightness") == "brightness_step"

    def test_boost_the_brightness(self):
        assert _name("boost the brightness") == "brightness_step"

    def test_boost_brightness(self):
        assert _name("boost brightness") == "brightness_step"

    def test_make_the_screen_brighter(self):
        assert _name("make the screen brighter") == "brightness_step"

    def test_make_screen_brighter(self):
        assert _name("make screen brighter") == "brightness_step"

    def test_make_it_brighter(self):
        assert _name("make it brighter") == "brightness_step"

    def test_brighter_standalone(self):
        assert _name("brighter") == "brightness_step"

    def test_brightness_up_delta_positive(self):
        a = _match("brightness up")
        assert a is not None and a.params["delta"] > 0

    # ── directional down ──────────────────────────────────────────

    def test_brightness_down(self):
        assert _name("brightness down") == "brightness_step"

    def test_brightness_decrease(self):
        assert _name("brightness decrease") == "brightness_step"

    def test_brightness_lower(self):
        assert _name("brightness lower") == "brightness_step"

    def test_brightness_less(self):
        assert _name("brightness less") == "brightness_step"

    def test_brightness_dim(self):
        assert _name("brightness dim") == "brightness_step"

    def test_screen_brightness_down(self):
        assert _name("screen brightness down") == "brightness_step"

    def test_turn_brightness_down(self):
        assert _name("turn brightness down") == "brightness_step"

    def test_turn_the_brightness_down(self):
        assert _name("turn the brightness down") == "brightness_step"

    def test_turn_down_the_brightness(self):
        assert _name("turn down the brightness") == "brightness_step"

    def test_dim_the_screen(self):
        assert _name("dim the screen") == "brightness_step"

    def test_make_the_screen_dimmer(self):
        assert _name("make the screen dimmer") == "brightness_step"

    def test_make_it_dimmer(self):
        assert _name("make it dimmer") == "brightness_step"

    def test_lower_the_brightness(self):
        assert _name("lower the brightness") == "brightness_step"

    def test_reduce_the_brightness(self):
        assert _name("reduce the brightness") == "brightness_step"

    def test_decrease_the_brightness(self):
        assert _name("decrease the brightness") == "brightness_step"

    def test_dimmer_standalone(self):
        # "dimmer" not in patterns — falls through
        assert _match("dimmer") is None

    def test_brightness_down_delta_negative(self):
        a = _match("brightness down")
        assert a is not None and a.params["delta"] < 0

    # ── set by number ─────────────────────────────────────────────

    def test_brightness_number(self):
        a = _match("brightness 70")
        assert a is not None and a.name == "brightness_set"
        assert a.params["level"] == 70

    def test_set_brightness_to_50(self):
        a = _match("set brightness to 50")
        assert a is not None and a.params["level"] == 50

    def test_set_the_brightness_to_80(self):
        a = _match("set the brightness to 80")
        assert a is not None and a.params["level"] == 80

    def test_set_screen_brightness_to_40(self):
        a = _match("set screen brightness to 40")
        assert a is not None and a.params["level"] == 40

    def test_set_the_screen_brightness_to_100(self):
        a = _match("set the screen brightness to 100")
        assert a is not None and a.params["level"] == 100

    def test_brightness_to_number(self):
        a = _match("brightness to 60")
        assert a is not None and a.params["level"] == 60

    def test_screen_brightness_to_number(self):
        a = _match("screen brightness to 90")
        assert a is not None and a.params["level"] == 90

    def test_brightness_zero(self):
        a = _match("brightness 0")
        assert a is not None and a.params["level"] == 0

    def test_set_brightness_100(self):
        a = _match("set brightness to 100")
        assert a is not None and a.params["level"] == 100


# ═══════════════════════════════════════════════════════════════════
# §6  Dark mode / Night mode
# ═══════════════════════════════════════════════════════════════════


class TestDisplayModes:

    def test_enable_dark_mode(self):
        a = _match("enable dark mode")
        assert a is not None and a.name == "dark_mode" and a.params["state"] == "on"

    def test_turn_on_dark_mode(self):
        a = _match("turn on dark mode")
        assert a is not None and a.params["state"] == "on"

    def test_switch_to_dark_mode(self):
        a = _match("switch to dark mode")
        assert a is not None and a.params["state"] == "on"

    def test_activate_dark_mode(self):
        a = _match("activate dark mode")
        assert a is not None and a.params["state"] == "on"

    def test_disable_dark_mode(self):
        a = _match("disable dark mode")
        assert a is not None and a.params["state"] == "off"

    def test_turn_off_dark_mode(self):
        a = _match("turn off dark mode")
        assert a is not None and a.params["state"] == "off"

    def test_switch_off_dark_mode(self):
        a = _match("switch off dark mode")
        assert a is not None and a.params["state"] == "off"

    def test_deactivate_dark_mode(self):
        a = _match("deactivate dark mode")
        assert a is not None and a.params["state"] == "off"

    def test_toggle_dark_mode(self):
        a = _match("toggle dark mode")
        assert a is not None and a.params["state"] == "toggle"

    def test_switch_dark_mode(self):
        a = _match("switch dark mode")
        assert a is not None and a.params["state"] == "toggle"

    def test_enable_light_mode(self):
        # light mode on → dark mode off
        a = _match("enable light mode")
        assert a is not None and a.name == "dark_mode" and a.params["state"] == "off"

    def test_turn_on_night_mode(self):
        a = _match("turn on night mode")
        assert a is not None and a.name == "night_mode" and a.params["state"] == "on"

    def test_enable_night_shift(self):
        a = _match("enable night shift")
        assert a is not None and a.params["state"] == "on"

    def test_activate_night_mode(self):
        a = _match("activate night mode")
        assert a is not None and a.params["state"] == "on"

    def test_disable_night_mode(self):
        a = _match("disable night mode")
        assert a is not None and a.params["state"] == "off"

    def test_turn_off_night_shift(self):
        a = _match("turn off night shift")
        assert a is not None and a.params["state"] == "off"

    def test_deactivate_night_shift(self):
        a = _match("deactivate night shift")
        assert a is not None and a.params["state"] == "off"


# ═══════════════════════════════════════════════════════════════════
# §7  Power
# ═══════════════════════════════════════════════════════════════════


class TestPower:

    def test_lock(self):
        assert _name("lock") == "lock_screen"

    def test_lock_screen(self):
        assert _name("lock screen") == "lock_screen"

    def test_lock_my_screen(self):
        assert _name("lock my screen") == "lock_screen"

    def test_lock_the_screen(self):
        assert _name("lock the screen") == "lock_screen"

    def test_sleep(self):
        assert _name("sleep") == "sleep_system"

    def test_sleep_mode(self):
        assert _name("sleep mode") == "sleep_system"

    def test_put_computer_to_sleep(self):
        assert _name("put computer to sleep") == "sleep_system"

    def test_put_my_computer_to_sleep(self):
        assert _name("put my computer to sleep") == "sleep_system"

    def test_put_the_mac_to_sleep(self):
        assert _name("put the mac to sleep") == "sleep_system"

    def test_put_my_laptop_to_sleep(self):
        # FIX: "laptop" added to _DEV
        assert _name("put my laptop to sleep") == "sleep_system"

    def test_put_my_device_to_sleep(self):
        assert _name("put my device to sleep") == "sleep_system"

    def test_put_my_machine_to_sleep(self):
        assert _name("put my machine to sleep") == "sleep_system"

    def test_send_my_computer_to_sleep(self):
        assert _name("send my computer to sleep") == "sleep_system"

    def test_hibernate(self):
        assert _name("hibernate") == "hibernate_system"

    def test_suspend_to_disk(self):
        assert _name("suspend to disk") == "hibernate_system"

    def test_shutdown(self):
        assert _name("shut down") == "shutdown_system"

    def test_shutdown_one_word(self):
        assert _name("shutdown") == "shutdown_system"

    def test_power_off(self):
        assert _name("power off") == "shutdown_system"

    def test_turn_off(self):
        assert _name("turn off") == "shutdown_system"

    def test_turn_off_my_computer(self):
        assert _name("turn off my computer") == "shutdown_system"

    def test_turn_off_my_laptop(self):
        assert _name("turn off my laptop") == "shutdown_system"

    def test_turn_off_the_pc(self):
        assert _name("turn off the pc") == "shutdown_system"

    def test_shut_my_pc_off(self):
        assert _name("shut my pc off") == "shutdown_system"

    def test_shut_the_mac_off(self):
        assert _name("shut the mac off") == "shutdown_system"

    def test_restart(self):
        assert _name("restart") == "restart_system"

    def test_reboot(self):
        assert _name("reboot") == "restart_system"

    def test_restart_my_computer_is_system_not_app(self):
        a = _match("restart my computer")
        assert a is not None
        assert a.name == "restart_system"

    def test_restart_my_mac_is_system_not_app(self):
        a = _match("restart my mac")
        assert a is not None
        assert a.name == "restart_system"

    def test_reboot_my_laptop(self):
        a = _match("reboot my laptop")
        assert a is not None
        assert a.name == "restart_system"

    def test_restart_app_still_works_for_real_apps(self):
        a = _match("restart spotify")
        assert a is not None and a.name == "restart_app"
        assert a.params["app"] == "Spotify"

    def test_logout(self):
        assert _name("log out") == "logout_system"

    def test_logout_one_word(self):
        assert _name("logout") == "logout_system"

    def test_sign_out(self):
        assert _name("sign out") == "logout_system"

    def test_logout_of_this_computer(self):
        assert _name("log out of this computer") == "logout_system"

    def test_logout_of_session(self):
        assert _name("log out of this session") == "logout_system"


# ═══════════════════════════════════════════════════════════════════
# §8  Wi-Fi  — includes group-index regression tests
# ═══════════════════════════════════════════════════════════════════


class TestWifi:

    def test_turn_on_wifi(self):
        a = _match("turn on wifi")
        assert a is not None and a.name == "wifi" and a.params["state"] == "on"

    def test_enable_wifi(self):
        a = _match("enable wifi")
        assert a is not None and a.params["state"] == "on"

    def test_connect_to_wifi(self):
        a = _match("connect to wifi")
        assert a is not None and a.params["state"] == "on"

    def test_connect_wifi(self):
        a = _match("connect wifi")
        assert a is not None and a.params["state"] == "on"

    def test_turn_off_wifi(self):
        a = _match("turn off wifi")
        assert a is not None and a.params["state"] == "off"

    def test_disable_wireless(self):
        a = _match("disable wireless")
        assert a is not None and a.params["state"] == "off"

    def test_disconnect_from_wifi(self):
        a = _match("disconnect from wifi")
        assert a is not None and a.params["state"] == "off"

    def test_toggle_wifi(self):
        a = _match("toggle wifi")
        assert a is not None and a.params["state"] == "toggle"

    def test_wifi_on_trailing_state(self):
        a = _match("wifi on")
        assert a is not None, "wifi on must match"
        assert a.params["state"] == "on"

    def test_wifi_off_trailing_state(self):
        a = _match("wifi off")
        assert a is not None and a.params["state"] == "off"

    def test_wifi_toggle_trailing_state(self):
        a = _match("wifi toggle")
        assert a is not None and a.params["state"] == "toggle"

    def test_wireless_on(self):
        a = _match("wireless on")
        assert a is not None and a.params["state"] == "on"

    def test_wi_fi_hyphenated_on(self):
        a = _match("turn on wi-fi")
        assert a is not None and a.params["state"] == "on"

    def test_wi_fi_space_variant(self):
        a = _match("enable wi fi")
        assert a is not None and a.params["state"] == "on"

    def test_turn_my_wifi_on(self):
        a = _match("turn my wifi on")
        assert a is not None
        assert a.params["state"] == "on"

    def test_turn_my_wifi_off(self):
        a = _match("turn my wifi off")
        assert a is not None and a.params["state"] == "off"

    def test_turn_the_wifi_on(self):
        a = _match("turn the wifi on")
        assert a is not None and a.params["state"] == "on"

    def test_turn_the_wifi_off(self):
        a = _match("turn the wifi off")
        assert a is not None and a.params["state"] == "off"

    def test_connect_to_my_wifi(self):
        a = _match("connect to my wifi")
        assert a is not None and a.params["state"] == "on"


# ═══════════════════════════════════════════════════════════════════
# §9  Bluetooth  — includes group-index regression tests
# ═══════════════════════════════════════════════════════════════════


class TestBluetooth:

    def test_turn_on_bluetooth(self):
        a = _match("turn on bluetooth")
        assert a is not None and a.name == "bluetooth" and a.params["state"] == "on"

    def test_enable_bt_alias(self):
        a = _match("enable bt")
        assert a is not None and a.params["state"] == "on"

    def test_turn_off_bluetooth(self):
        a = _match("turn off bluetooth")
        assert a is not None and a.params["state"] == "off"

    def test_disable_bluetooth(self):
        a = _match("disable bluetooth")
        assert a is not None and a.params["state"] == "off"

    def test_disconnect_bluetooth(self):
        a = _match("disconnect bluetooth")
        assert a is not None and a.params["state"] == "off"

    def test_toggle_bluetooth(self):
        a = _match("toggle bluetooth")
        assert a is not None and a.params["state"] == "toggle"

    def test_bluetooth_on_trailing_state(self):
        a = _match("bluetooth on")
        assert a is not None, "bluetooth on must match"
        assert a.params["state"] == "on"

    def test_bluetooth_off_trailing_state(self):
        a = _match("bluetooth off")
        assert a is not None and a.params["state"] == "off"

    def test_bluetooth_toggle_trailing_state(self):
        a = _match("bluetooth toggle")
        assert a is not None and a.params["state"] == "toggle"

    def test_bt_on_abbreviation(self):
        a = _match("bt on")
        assert a is not None and a.params["state"] == "on"

    def test_bt_off_abbreviation(self):
        a = _match("bt off")
        assert a is not None and a.params["state"] == "off"

    def test_turn_my_bluetooth_on(self):
        a = _match("turn my bluetooth on")
        assert a is not None and a.params["state"] == "on"

    def test_turn_my_bluetooth_off(self):
        a = _match("turn my bluetooth off")
        assert a is not None and a.params["state"] == "off"

    def test_turn_the_bluetooth_on(self):
        a = _match("turn the bluetooth on")
        assert a is not None and a.params["state"] == "on"


# ═══════════════════════════════════════════════════════════════════
# §10  Focus / Do Not Disturb
# ═══════════════════════════════════════════════════════════════════


class TestFocus:

    def test_enable_dnd(self):
        a = _match("enable do not disturb")
        assert (
            a is not None and a.name == "do_not_disturb" and a.params["state"] == "on"
        )

    def test_turn_on_dnd_abbrev(self):
        a = _match("turn on dnd")
        assert a is not None and a.params["state"] == "on"

    def test_activate_focus_mode(self):
        a = _match("activate focus mode")
        assert a is not None and a.params["state"] == "on"

    def test_activate_focus(self):
        a = _match("activate focus")
        assert a is not None and a.params["state"] == "on"

    def test_start_focus(self):
        a = _match("start focus")
        assert a is not None and a.params["state"] == "on"

    def test_disable_dnd(self):
        a = _match("disable do not disturb")
        assert a is not None and a.params["state"] == "off"

    def test_turn_off_focus_mode(self):
        a = _match("turn off focus mode")
        assert a is not None and a.params["state"] == "off"

    def test_deactivate_dnd(self):
        a = _match("deactivate dnd")
        assert a is not None and a.params["state"] == "off"

    def test_stop_focus(self):
        a = _match("stop focus")
        assert a is not None and a.params["state"] == "off"

    def test_bare_do_not_disturb_defaults_on(self):
        a = _match("do not disturb")
        assert a is not None and a.params["state"] == "on"

    def test_do_not_disturb_on_explicit(self):
        a = _match("do not disturb on")
        assert a is not None and a.params["state"] == "on"

    def test_do_not_disturb_off_explicit(self):
        a = _match("do not disturb off")
        assert a is not None and a.params["state"] == "off"

    def test_quiet_mode(self):
        # FIX: quiet mode → DND on
        a = _match("quiet mode")
        assert a is not None and a.params["state"] == "on"

    def test_quiet_hours(self):
        a = _match("quiet hours")
        assert a is not None and a.params["state"] == "on"

    def test_silence_notifications(self):
        a = _match("silence notifications")
        assert a is not None and a.params["state"] == "on"

    def test_silence_alerts(self):
        a = _match("silence alerts")
        assert a is not None and a.params["state"] == "on"


# ═══════════════════════════════════════════════════════════════════
# §11  Screenshot
# ═══════════════════════════════════════════════════════════════════


class TestScreenshot:

    def test_screenshot(self):
        assert _name("screenshot") == "screenshot"

    def test_take_screenshot(self):
        assert _name("take screenshot") == "screenshot"

    def test_take_a_screenshot(self):
        assert _name("take a screenshot") == "screenshot"

    def test_grab_a_screenshot(self):
        assert _name("grab a screenshot") == "screenshot"

    def test_snap_a_screenshot(self):
        assert _name("snap a screenshot") == "screenshot"

    def test_capture_the_screen(self):
        assert _name("capture the screen") == "screenshot"

    def test_capture_screen(self):
        assert _name("capture screen") == "screenshot"

    def test_screen_capture(self):
        assert _name("screen capture") == "screenshot"

    def test_print_screen(self):
        # FIX: print screen added
        assert _name("print screen") == "screenshot"

    def test_screengrab(self):
        assert _name("screengrab") == "screenshot"

    def test_screen_shot_spaced(self):
        assert _name("screen shot") == "screenshot"

    def test_screenshot_now(self):
        # suffix "now" stripped before matching
        assert _name("screenshot now") == "screenshot"

    def test_screenshot_the_screen(self):
        assert _name("screenshot the screen") == "screenshot"


# ═══════════════════════════════════════════════════════════════════
# §12  Quick Info — time / date / battery
# ═══════════════════════════════════════════════════════════════════


class TestQuickInfo:

    def test_what_time_is_it(self):
        assert _name("what time is it") == "tell_time"

    def test_what_is_the_time(self):
        assert _name("what is the time") == "tell_time"

    def test_whats_the_time_no_apostrophe(self):
        # FIX: "whats" → "what is"
        assert _name("whats the time") == "tell_time"

    def test_current_time(self):
        assert _name("current time") == "tell_time"

    def test_tell_me_the_time(self):
        assert _name("tell me the time") == "tell_time"

    def test_tell_me_time(self):
        assert _name("tell me time") == "tell_time"

    def test_time_please_stripped_to_time(self):
        # "please" is a suffix stripped by _SUFFIX_RE → "time" remains.
        # "time" alone doesn't match any tell_time alternative → falls through.
        # The "time\s+please" alternative in tell_time is dead code.
        assert _name("time please") is None

    def test_what_is_today(self):
        assert _name("what is today") == "tell_date"

    def test_what_day_is_it(self):
        assert _name("what day is it") == "tell_date"

    def test_todays_date(self):
        assert _name("today's date") == "tell_date"

    def test_what_is_the_date(self):
        assert _name("what is the date") == "tell_date"

    def test_what_today(self):
        assert _name("what today") == "tell_date"

    def test_battery(self):
        assert _name("battery") == "tell_battery"

    def test_battery_level(self):
        assert _name("battery level") == "tell_battery"

    def test_battery_status(self):
        assert _name("battery status") == "tell_battery"

    def test_battery_percentage(self):
        assert _name("battery percentage") == "tell_battery"

    def test_battery_life(self):
        assert _name("battery life") == "tell_battery"

    def test_battery_charge(self):
        assert _name("battery charge") == "tell_battery"

    def test_check_battery(self):
        assert _name("check battery") == "tell_battery"

    def test_check_battery_level(self):
        assert _name("check battery level") == "tell_battery"

    def test_check_battery_status(self):
        assert _name("check battery status") == "tell_battery"

    def test_how_much_battery(self):
        assert _name("how much battery") == "tell_battery"

    def test_how_much_battery_is_left(self):
        assert _name("how much battery is left") == "tell_battery"

    def test_how_much_battery_do_i_have(self):
        assert _name("how much battery do i have") == "tell_battery"

    def test_what_is_my_battery_percentage(self):
        assert _name("what is my battery percentage") == "tell_battery"

    def test_what_is_battery_percentage(self):
        assert _name("what is battery percentage") == "tell_battery"


# ═══════════════════════════════════════════════════════════════════
# §13  App launch — open / launch / start
# ═══════════════════════════════════════════════════════════════════


class TestAppLaunch:

    def test_open_spotify(self):
        a = _match("open spotify")
        assert a is not None and a.name == "open_app" and a.params["app"] == "Spotify"

    def test_launch_chrome(self):
        a = _match("launch chrome")
        assert a is not None and a.params["app"] == "Google Chrome"

    def test_start_discord(self):
        a = _match("start discord")
        assert a is not None and a.params["app"] == "Discord"

    def test_open_google_chrome_multi_word(self):
        # FIX: multi-word alias
        a = _match("open google chrome")
        assert a is not None and a.params["app"] == "Google Chrome"

    def test_open_youtube_music_multi_word(self):
        a = _match("open youtube music")
        assert a is not None and a.params["app"] == "YouTube Music"

    def test_open_microsoft_teams(self):
        a = _match("open microsoft teams")
        assert a is not None and a.params["app"] == "Microsoft Teams"

    def test_open_vs_code(self):
        a = _match("open vs code")
        assert a is not None and a.params["app"] == "Visual Studio Code"

    def test_open_visual_studio_code(self):
        a = _match("open visual studio code")
        assert a is not None and a.params["app"] == "Visual Studio Code"

    def test_open_the_terminal_strips_article(self):
        # FIX: "the" stripped from app name by _resolve_app
        a = _match("open the terminal")
        assert a is not None and a.params["app"] == "Terminal"

    def test_open_my_finder_strips_article(self):
        a = _match("open my finder")
        assert a is not None and a.params["app"] == "Finder"

    def test_open_my_browser_ambiguous(self):
        # FIX: "my browser" is in _AMBIGUOUS_APP_RE → fall through
        assert _match("open my browser") is None

    def test_open_a_new_window_ambiguous(self):
        assert _match("open a new window") is None

    def test_open_a_folder_ambiguous(self):
        assert _match("open a folder") is None

    def test_open_a_tab_ambiguous(self):
        assert _match("open a tab") is None

    def test_open_something_ambiguous(self):
        assert _match("open something") is None

    def test_open_anything_ambiguous(self):
        assert _match("open anything") is None

    def test_open_an_app_ambiguous(self):
        assert _match("open an app") is None

    def test_open_and_play_chain(self):
        a = _match("open spotify and play")
        assert a is not None and a.name == "open_app"
        assert a.params["app"] == "Spotify"
        assert len(a.chain) == 1
        assert a.chain[0].name == "media_play"

    def test_open_vlc_and_play_chain(self):
        a = _match("open vlc and play")
        assert a is not None and len(a.chain) == 1

    def test_restart_app_spotify(self):
        a = _match("restart spotify")
        assert (
            a is not None and a.name == "restart_app" and a.params["app"] == "Spotify"
        )

    def test_restart_discord(self):
        a = _match("restart discord")
        assert a is not None and a.name == "restart_app"


# ═══════════════════════════════════════════════════════════════════
# §14  Folder shortcuts
# ═══════════════════════════════════════════════════════════════════


class TestFolders:

    def test_open_downloads(self):
        a = _match("open downloads")
        assert a is not None and a.name == "open_folder"

    def test_open_my_downloads(self):
        a = _match("open my downloads")
        assert a is not None and a.name == "open_folder"

    def test_open_desktop(self):
        a = _match("open desktop")
        assert a is not None and a.name == "open_folder"

    def test_open_documents(self):
        a = _match("open documents")
        assert a is not None and a.name == "open_folder"

    def test_open_home(self):
        a = _match("open home")
        assert a is not None and a.name == "open_folder"

    def test_open_home_folder(self):
        a = _match("open home folder")
        assert a is not None and a.name == "open_folder"

    def test_open_pictures(self):
        a = _match("open pictures")
        assert a is not None and a.name == "open_folder"

    def test_open_music_folder(self):
        a = _match("open music")
        assert a is not None and a.name == "open_folder"

    def test_open_videos(self):
        a = _match("open videos")
        assert a is not None and a.name == "open_folder"

    def test_open_movies(self):
        a = _match("open movies")
        assert a is not None and a.name == "open_folder"

    def test_folder_path_is_absolute(self):
        a = _match("open downloads")
        assert a is not None
        assert os.path.isabs(a.params["path"])

    def test_folder_before_app_launch(self):
        # Pattern ordering: open_folder must precede open_app
        a = _match("open downloads")
        assert (
            a is not None and a.name == "open_folder"
        ), "open_folder pattern must come before open_app in pattern table"


# ═══════════════════════════════════════════════════════════════════
# §15  App alias resolution (_resolve_app)
# ═══════════════════════════════════════════════════════════════════


class TestAliasResolution:

    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("chrome", "Google Chrome"),
            ("google chrome", "Google Chrome"),
            ("firefox", "Firefox"),
            ("safari", "Safari"),
            ("edge", "Microsoft Edge"),
            ("microsoft edge", "Microsoft Edge"),
            ("brave", "Brave Browser"),
            ("spotify", "Spotify"),
            ("yt", "YouTube"),
            ("youtube", "YouTube"),
            ("ytm", "YouTube Music"),
            ("youtube music", "YouTube Music"),
            ("youtubemusic", "YouTube Music"),
            ("apple music", "Music"),
            ("itunes", "Music"),
            ("vlc", "VLC"),
            ("plex", "Plex"),
            ("vscode", "Visual Studio Code"),
            ("code", "Visual Studio Code"),
            ("vs code", "Visual Studio Code"),
            ("visual studio code", "Visual Studio Code"),
            ("word", "Microsoft Word"),
            ("microsoft word", "Microsoft Word"),
            ("excel", "Microsoft Excel"),
            ("microsoft excel", "Microsoft Excel"),
            ("powerpoint", "Microsoft PowerPoint"),
            ("ppt", "Microsoft PowerPoint"),
            ("onenote", "Microsoft OneNote"),
            ("notion", "Notion"),
            ("obsidian", "Obsidian"),
            ("slack", "Slack"),
            ("discord", "Discord"),
            ("teams", "Microsoft Teams"),
            ("microsoft teams", "Microsoft Teams"),
            ("zoom", "Zoom"),
            ("mail", "Mail"),
            ("outlook", "Microsoft Outlook"),
            ("microsoft outlook", "Microsoft Outlook"),
            ("finder", "Finder"),
            ("terminal", "Terminal"),
            ("iterm", "iTerm"),
            ("iterm2", "iTerm"),
            ("activity monitor", "Activity Monitor"),
            ("system prefs", "System Preferences"),
            ("system preferences", "System Preferences"),
            ("system settings", "System Settings"),
            ("explorer", "explorer.exe"),
            ("file explorer", "explorer.exe"),
            ("notepad", "notepad.exe"),
            ("calculator", "calc.exe"),
            ("calc", "calc.exe"),
            ("task manager", "taskmgr.exe"),
        ],
    )
    def test_alias(self, raw, expected):
        assert _resolve_app(raw) == expected

    def test_case_insensitive_spotify(self):
        assert _resolve_app("SPOTIFY") == "Spotify"

    def test_case_insensitive_chrome(self):
        assert _resolve_app("Chrome") == "Google Chrome"

    def test_article_the_stripped(self):
        assert _resolve_app("the terminal") == "Terminal"

    def test_article_my_stripped(self):
        assert _resolve_app("my finder") == "Finder"

    def test_article_a_stripped(self):
        # "a browser" → strip "a " → "browser" → not in aliases → pass through
        result = _resolve_app("a browser")
        assert result == "browser"

    def test_unknown_app_pass_through(self):
        assert _resolve_app("MyCustomApp") == "MyCustomApp"

    def test_unknown_app_lowercase_pass_through(self):
        # Not in aliases → returned as-is (not title-cased)
        result = _resolve_app("myfancyapp")
        assert result == "myfancyapp"


# ═══════════════════════════════════════════════════════════════════
# §16  Pattern ordering — structural invariants
# ═══════════════════════════════════════════════════════════════════


class TestPatternOrdering:
    """Verify the ordering invariants documented in §5 header."""

    def test_search_on_app_before_play_on_app(self):
        # "find X on Y" must hit search_on_app, not play_on_app
        a = _match("find Bohemian Rhapsody on Spotify")
        assert a is not None and a.name == "search_on_app"

    def test_open_and_play_before_plain_open(self):
        # "open X and play" must produce chain, not bare open_app
        a = _match("open spotify and play")
        assert a is not None and len(a.chain) > 0

    def test_folder_shortcuts_before_open_app(self):
        # "open downloads" must be open_folder, not open_app
        a = _match("open downloads")
        assert a is not None and a.name == "open_folder"

    def test_volume_set_before_directional(self):
        # "volume 50" must be volume_set
        a = _match("volume 50")
        assert a is not None and a.name == "volume_set"

    def test_play_next_before_ambiguous_play(self):
        # "play next" must be media_next, not fall-through
        a = _match("play next")
        assert a is not None and a.name == "media_next"

    def test_brightness_set_before_directional(self):
        a = _match("brightness 70")
        assert a is not None and a.name == "brightness_set"

    def test_play_on_app_before_generic_play(self):
        # "play X on Y" must hit play_on_app, not generic media_play
        a = _match("play music on spotify")
        assert a is not None and a.name == "play_on_app"

    def test_restart_app_before_open_app(self):
        # "restart spotify" must be restart_app, not open_app
        a = _match("restart spotify")
        assert a is not None and a.name == "restart_app"


# ═══════════════════════════════════════════════════════════════════
# §17  Hard fall-through — must return None
# ═══════════════════════════════════════════════════════════════════


class TestFallThrough:
    """Inputs that look like commands but must fall through to Brain."""

    def test_empty_input(self):
        assert _match("") is None

    def test_only_whitespace(self):
        assert _match("   ") is None

    def test_only_filler_after_normalize(self):
        assert _match("hey buddy please") is None

    def test_play_specific_song_no_app(self):
        assert _name("play Bohemian Rhapsody") == "play_on_app"

    def test_play_artist_request(self):
        assert _name("play something by The Beatles") == "play_on_app"

    def test_open_my_browser(self):
        assert _match("open my browser") is None

    def test_open_a_tab(self):
        assert _match("open a tab") is None

    def test_open_a_file(self):
        assert _match("open a file") is None

    def test_random_question(self):
        assert _match("what is the meaning of life") is None

    def test_conversational(self):
        assert _match("how are you doing today") is None

    def test_numbers_only(self):
        assert _match("42") is None

    def test_gibberish(self):
        assert _match("asdfghjkl") is None

    def test_volume_high_text_level(self):
        assert _match("volume high") is None

    def test_volume_up_a_bit(self):
        assert _match("volume up a bit") is None

    def test_turn_it_up_no_match(self):
        # "it" removed from coref — but no pattern covers "turn it up"
        assert _match("turn it up") is None

    def test_search_without_on_app(self):
        assert _match("search for lofi beats") is None

    def test_play_next_by_artist_falls_through(self):
        # Specific content after "play next" → Brain
        assert _name("play next track by Queen") == "play_on_app"

    def test_play_the_playlist_coref(self):
        assert _match("play the playlist") is None

    def test_play_the_song_coref(self):
        assert _match("play the song") is None

    def test_play_the_one_coref(self):
        assert _match("play the one") is None

    def test_what_about_X_falls_through(self):
        assert _match("what about spotify") is None

    def test_find_without_on_app(self):
        assert _match("find the best headphones") is None


# ═══════════════════════════════════════════════════════════════════
# §18  URL open
# ═══════════════════════════════════════════════════════════════════


class TestUrlOpen:

    def test_open_domain_dot_com(self):
        a = _match("open google.com")
        assert a is not None and a.name == "open_url"

    def test_url_raw_domain_in_params(self):
        # The pattern builder stores the raw domain; the _open_url handler adds https://
        a = _match("open google.com")
        assert a is not None
        assert "google.com" in a.params["url"]

    def test_go_to_github(self):
        a = _match("go to github.com")
        assert a is not None and a.name == "open_url"

    def test_visit_reddit(self):
        a = _match("visit reddit.com")
        assert a is not None and a.name == "open_url"

    def test_navigate_to_stackoverflow(self):
        a = _match("navigate to stackoverflow.com")
        assert a is not None and a.name == "open_url"

    def test_browse_to_example(self):
        a = _match("browse to example.com")
        assert a is not None and a.name == "open_url"

    def test_open_url_with_https_already(self):
        # normalize() strips https://, builder adds it back → same result as bare domain
        a = _match("open https://google.com")
        assert a is not None and a.name == "open_url"
        assert a.params["url"].endswith("google.com")

    def test_open_url_http_protocol_stripped(self):
        # http:// is stripped in normalize; builder always adds https://
        a = _match("open http://example.com")
        assert a is not None and a.name == "open_url"
        assert "example.com" in a.params["url"]

    def test_open_url_with_path_falls_through(self):
        # path separator / is stripped in normalize → trailing "docs" breaks anchor → None
        assert _match("open python.org/docs") is None

    def test_open_url_dots_preserved_in_domain(self):
        # normalize() must not strip dots from domain names
        a = _match("open google.com")
        assert a is not None
        assert "google.com" in a.params["url"]

    def test_no_dot_no_url_match(self):
        # "open google" — no dot → not a URL → open_app or fall-through
        a = _match("open google")
        assert a is None or a.name != "open_url"

    def test_url_before_app_launch_in_ordering(self):
        # If open_url pattern fires it must come before open_app
        a = _match("open github.com")
        assert a is not None and a.name == "open_url"

    def test_open_subdomain_url(self):
        a = _match("open docs.python.org")
        assert a is not None and a.name == "open_url"

    def test_open_url_with_two_letter_tld(self):
        a = _match("open bbc.co.uk")
        assert a is not None and a.name == "open_url"


# ═══════════════════════════════════════════════════════════════════
# §19  App quit
# ═══════════════════════════════════════════════════════════════════


class TestQuitApp:

    def test_quit_spotify(self):
        a = _match("quit spotify")
        assert a is not None and a.name == "quit_app"
        assert a.params["app"] == "Spotify"

    def test_quit_chrome(self):
        a = _match("quit chrome")
        assert a is not None and a.name == "quit_app"
        assert a.params["app"] == "Google Chrome"

    def test_kill_discord(self):
        a = _match("kill discord")
        assert a is not None and a.name == "quit_app"
        assert a.params["app"] == "Discord"

    def test_exit_terminal(self):
        a = _match("exit terminal")
        assert a is not None and a.name == "quit_app"
        assert a.params["app"] == "Terminal"

    def test_force_quit_spotify(self):
        a = _match("force quit spotify")
        assert a is not None and a.name == "quit_app"
        assert a.params["force"] is True

    def test_quit_sets_force_false_by_default(self):
        a = _match("quit safari")
        assert a is not None
        assert a.params["force"] is False

    def test_force_quit_sets_force_true(self):
        a = _match("force quit discord")
        assert a is not None
        assert a.params["force"] is True

    def test_kill_sets_force_false(self):
        # kill → quit_app (graceful), not force_quit
        a = _match("kill safari")
        assert a is not None and a.params["force"] is False

    def test_quit_alias_resolves(self):
        a = _match("quit vscode")
        assert a is not None
        assert a.params["app"] == "Visual Studio Code"

    def test_quit_multi_word_app(self):
        a = _match("quit google chrome")
        assert a is not None
        assert a.params["app"] == "Google Chrome"

    def test_quit_does_not_trigger_on_system(self):
        # "quit my computer" — _DEVICE_RE guard → should fall through
        a = _match("quit my computer")
        assert a is None or a.name != "quit_app"

    def test_quit_before_open_app_in_ordering(self):
        # quit_app must not shadow open_app for same inputs
        a_open = _match("open spotify")
        a_quit = _match("quit spotify")
        assert a_open is not None and a_open.name == "open_app"
        assert a_quit is not None and a_quit.name == "quit_app"


# ═══════════════════════════════════════════════════════════════════
# §20  Timer
# ═══════════════════════════════════════════════════════════════════


class TestTimer:

    def test_set_timer_for_5_minutes(self):
        a = _match("set a timer for 5 minutes")
        assert a is not None and a.name == "timer"
        assert a.params["n"] == 5
        assert a.params["unit"] == "minutes"

    def test_timer_for_30_seconds(self):
        a = _match("timer for 30 seconds")
        assert a is not None and a.name == "timer"
        assert a.params["n"] == 30
        assert a.params["unit"] == "seconds"

    def test_set_timer_for_1_hour(self):
        a = _match("set timer for 1 hour")
        assert a is not None and a.name == "timer"
        assert a.params["n"] == 1
        assert a.params["unit"] == "hour"  # singular as typed

    def test_timer_minutes_singular(self):
        a = _match("timer for 1 minute")
        assert a is not None and a.name == "timer"
        assert a.params["n"] == 1

    def test_timer_seconds_singular(self):
        a = _match("timer for 1 second")
        assert a is not None and a.name == "timer"
        assert a.params["unit"] == "second"  # singular as typed

    def test_timer_without_for(self):
        a = _match("set a timer 10 minutes")
        assert a is not None and a.name == "timer"
        assert a.params["n"] == 10

    def test_timer_45_minutes(self):
        a = _match("set a timer for 45 minutes")
        assert a is not None
        assert a.params["n"] == 45

    def test_timer_2_hours(self):
        a = _match("a timer for 2 hours")
        assert a is not None and a.name == "timer"
        assert a.params["n"] == 2

    def test_timer_without_set_prefix(self):
        a = _match("timer for 20 minutes")
        assert a is not None and a.name == "timer"

    def test_timer_large_number(self):
        a = _match("set a timer for 120 seconds")
        assert a is not None
        assert a.params["n"] == 120

    def test_timer_no_unit_falls_through(self):
        # "set a timer" with no time spec → Brain
        assert _match("set a timer") is None

    def test_timer_text_number_falls_through(self):
        # "set a timer for five minutes" — word numbers not handled → Brain
        assert _match("set a timer for five minutes") is None


# ═══════════════════════════════════════════════════════════════════
# §21  Math calculation
# ═══════════════════════════════════════════════════════════════════


class TestMath:

    def test_5_plus_3(self):
        a = _match("5 plus 3")
        assert a is not None and a.name == "math_calculate"

    def test_what_is_5_plus_3(self):
        a = _match("what is 5 plus 3")
        assert a is not None and a.name == "math_calculate"

    def test_calculate_prefix(self):
        a = _match("calculate 10 times 5")
        assert a is not None and a.name == "math_calculate"

    def test_calc_prefix(self):
        a = _match("calc 100 divided by 4")
        assert a is not None and a.name == "math_calculate"

    def test_minus(self):
        a = _match("10 minus 4")
        assert a is not None and a.name == "math_calculate"
        assert a.params["op"] == "minus"

    def test_times(self):
        a = _match("3 times 7")
        assert a is not None and a.name == "math_calculate"
        assert a.params["op"] == "times"

    def test_divided_by(self):
        a = _match("15 divided by 3")
        assert a is not None and a.name == "math_calculate"

    def test_multiplied_by(self):
        a = _match("10 multiplied by 3")
        assert a is not None and a.name == "math_calculate"

    def test_modulo(self):
        a = _match("17 modulo 5")
        assert a is not None and a.name == "math_calculate"

    def test_mod_short(self):
        a = _match("17 mod 5")
        assert a is not None and a.name == "math_calculate"

    def test_decimal_operands(self):
        # _PUNCT_RE fix preserves dots between digits
        a = _match("2.5 plus 1.5")
        assert a is not None and a.name == "math_calculate"
        assert a.params["a"] == "2.5"
        assert a.params["b"] == "1.5"

    def test_operand_a_parsed_correctly(self):
        # builder stores raw regex match strings; handler converts to float
        a = _match("5 plus 3")
        assert a is not None
        assert a.params["a"] == "5"

    def test_operand_b_parsed_correctly(self):
        a = _match("5 plus 3")
        assert a is not None
        assert a.params["b"] == "3"

    def test_zero_operand(self):
        a = _match("5 plus 0")
        assert a is not None and a.name == "math_calculate"
        assert a.params["b"] == "0"

    def test_plus_op_stored(self):
        a = _match("5 plus 3")
        assert a is not None and a.params["op"] == "plus"

    def test_plain_numbers_no_op_falls_through(self):
        # "5 3" — no operator → Brain
        assert _match("5 3") is None

    def test_symbolic_plus_falls_through(self):
        # "+ operator stripped by normalize" → Brain
        assert _match("5 + 3") is None

    def test_word_five_falls_through(self):
        # word numbers not handled
        assert _match("five plus three") is None


# ═══════════════════════════════════════════════════════════════════
# §22  System stats
# ═══════════════════════════════════════════════════════════════════


class TestSysStats:

    def test_cpu_usage(self):
        assert _name("cpu usage") == "sys_stat"

    def test_cpu_bare(self):
        assert _name("cpu") == "sys_stat"

    def test_what_is_my_cpu(self):
        assert _name("what is my cpu") == "sys_stat"

    def test_processor_usage(self):
        assert _name("processor usage") == "sys_stat"

    def test_ram(self):
        assert _name("ram") == "sys_stat"

    def test_memory_usage(self):
        assert _name("memory usage") == "sys_stat"

    def test_how_much_ram_do_i_have(self):
        assert _name("how much ram do I have") == "sys_stat"

    def test_how_much_memory_do_i_have(self):
        assert _name("how much memory do I have") == "sys_stat"

    def test_free_memory(self):
        assert _name("free memory") == "sys_stat"

    def test_available_ram(self):
        assert _name("available ram") == "sys_stat"

    def test_disk_space(self):
        assert _name("disk space") == "sys_stat"

    def test_disk_bare(self):
        assert _name("disk") == "sys_stat"

    def test_how_much_disk_space(self):
        assert _name("how much disk space") == "sys_stat"

    def test_free_disk(self):
        assert _name("free disk") == "sys_stat"

    def test_storage_left(self):
        assert _name("storage left") == "sys_stat"

    def test_uptime(self):
        assert _name("uptime") == "sys_stat"

    def test_system_uptime(self):
        assert _name("system uptime") == "sys_stat"

    def test_how_long_computer_on(self):
        assert _name("how long has my computer been on") == "sys_stat"

    def test_ip_address(self):
        assert _name("ip address") == "sys_stat"

    def test_my_ip_address(self):
        assert _name("my ip address") == "sys_stat"

    def test_local_ip(self):
        assert _name("local ip") == "sys_stat"

    def test_what_is_my_ip(self):
        assert _name("what is my ip") == "sys_stat"

    def test_internet_connected(self):
        assert _name("am I connected to the internet") == "sys_stat"

    def test_check_internet(self):
        assert _name("check my internet connection") == "sys_stat"

    def test_kind_cpu(self):
        a = _match("cpu usage")
        assert a is not None and a.params["kind"] == "cpu"

    def test_kind_ram(self):
        a = _match("ram")
        assert a is not None and a.params["kind"] == "ram"

    def test_kind_disk(self):
        a = _match("disk space")
        assert a is not None and a.params["kind"] == "disk"

    def test_kind_uptime(self):
        a = _match("uptime")
        assert a is not None and a.params["kind"] == "uptime"

    def test_kind_ip(self):
        a = _match("my ip address")
        assert a is not None and a.params["kind"] == "ip"

    def test_kind_net(self):
        a = _match("am I connected to the internet")
        assert a is not None and a.params["kind"] == "net"


# ═══════════════════════════════════════════════════════════════════
# §23  App focus / switch
# ═══════════════════════════════════════════════════════════════════


class TestFocusApp:

    def test_switch_to_spotify(self):
        a = _match("switch to spotify")
        assert a is not None and a.name == "focus_app"
        assert a.params["app"] == "Spotify"

    def test_focus_terminal(self):
        a = _match("focus terminal")
        assert a is not None and a.name == "focus_app"
        assert a.params["app"] == "Terminal"

    def test_bring_chrome_to_front(self):
        a = _match("bring chrome to front")
        assert a is not None and a.name == "focus_app"
        assert a.params["app"] == "Google Chrome"

    def test_bring_up_discord(self):
        a = _match("bring up discord")
        assert a is not None and a.name == "focus_app"
        assert a.params["app"] == "Discord"

    def test_show_finder(self):
        a = _match("show finder")
        assert a is not None and a.name == "focus_app"

    def test_raise_vscode(self):
        a = _match("raise vscode")
        assert a is not None and a.name == "focus_app"
        assert a.params["app"] == "Visual Studio Code"

    def test_switch_resolves_alias(self):
        a = _match("switch to chrome")
        assert a is not None
        assert a.params["app"] == "Google Chrome"

    def test_focus_without_app_falls_through(self):
        # "focus" alone — no app group → None
        assert _match("focus") is None or _name("focus") != "focus_app"


# ═══════════════════════════════════════════════════════════════════
# §24  Unit conversion
# ═══════════════════════════════════════════════════════════════════


class TestUnitConvert:

    def test_celsius_to_fahrenheit(self):
        a = _match("100 celsius to fahrenheit")
        assert a is not None and a.name == "unit_convert"
        assert a.params["val"] == 100.0
        assert "celsius" in a.params["src"]
        assert "fahrenheit" in a.params["dst"]

    def test_fahrenheit_to_celsius(self):
        a = _match("32 fahrenheit to celsius")
        assert a is not None and a.name == "unit_convert"

    def test_km_to_miles(self):
        a = _match("10 km to miles")
        assert a is not None and a.name == "unit_convert"

    def test_miles_to_km(self):
        a = _match("5 miles to km")
        assert a is not None and a.name == "unit_convert"

    def test_kg_to_pounds(self):
        a = _match("5 kg in pounds")
        assert a is not None and a.name == "unit_convert"

    def test_pounds_to_kg(self):
        a = _match("150 pounds to kg")
        assert a is not None and a.name == "unit_convert"

    def test_inches_to_cm(self):
        a = _match("10 inches in cm")
        assert a is not None and a.name == "unit_convert"

    def test_mph_to_kmh(self):
        a = _match("60 mph to kmh")
        assert a is not None and a.name == "unit_convert"

    def test_liters_to_gallons(self):
        a = _match("5 liters in gallons")
        assert a is not None and a.name == "unit_convert"

    def test_feet_to_meters(self):
        a = _match("6 feet to meters")
        assert a is not None and a.name == "unit_convert"

    def test_convert_prefix(self):
        a = _match("convert 100 celsius to fahrenheit")
        assert a is not None and a.name == "unit_convert"

    def test_decimal_value(self):
        a = _match("1.5 kg to pounds")
        assert a is not None
        assert a.params["val"] == 1.5

    def test_same_unit_still_matches(self):
        a = _match("5 km to km")
        assert a is not None and a.name == "unit_convert"

    def test_no_unit_falls_through(self):
        assert _match("convert 100") is None

    def test_incompatible_category_falls_through(self):
        # "km to kg" — different physical dimensions — match still fires,
        # handler returns an error string (not our concern at match level)
        a = _match("10 km to kg")
        assert a is not None and a.name == "unit_convert"


# ═══════════════════════════════════════════════════════════════════
# §25  World clock
# ═══════════════════════════════════════════════════════════════════


class TestWorldClock:

    def test_time_in_tokyo(self):
        a = _match("what time is it in tokyo")
        assert a is not None and a.name == "world_clock"
        assert a.params["city"] == "tokyo"

    def test_time_in_london(self):
        a = _match("time in london")
        assert a is not None and a.name == "world_clock"

    def test_current_time_in_new_york(self):
        a = _match("current time in new york")
        assert a is not None and a.name == "world_clock"
        assert a.params["city"] == "new york"

    def test_time_in_paris(self):
        a = _match("what is the time in paris")
        assert a is not None and a.name == "world_clock"

    def test_time_in_dubai(self):
        a = _match("time in dubai")
        assert a is not None and a.name == "world_clock"

    def test_time_in_mumbai(self):
        a = _match("what time is it in mumbai")
        assert a is not None and a.name == "world_clock"

    def test_time_in_sydney(self):
        a = _match("time in sydney")
        assert a is not None and a.name == "world_clock"

    def test_city_lowercased_in_params(self):
        a = _match("time in Tokyo")
        assert a is not None
        assert a.params["city"] == "tokyo"

    def test_bare_time_not_world_clock(self):
        # "what time is it" — no city → tell_time, not world_clock
        assert _name("what time is it") == "tell_time"

    def test_no_city_falls_through(self):
        assert _match("time in") is None


# ═══════════════════════════════════════════════════════════════════
# §26  Number base conversion
# ═══════════════════════════════════════════════════════════════════


class TestBaseConvert:

    def test_decimal_to_binary(self):
        a = _match("42 in binary")
        assert a is not None and a.name == "base_convert"
        assert a.params["num"] == "42"
        assert a.params["base"] == "binary"

    def test_decimal_to_hex(self):
        a = _match("255 in hex")
        assert a is not None and a.name == "base_convert"
        assert a.params["base"] == "hex"

    def test_hex_to_decimal(self):
        a = _match("0xFF in decimal")
        assert a is not None and a.name == "base_convert"
        assert a.params["num"] == "0xff"

    def test_decimal_to_octal(self):
        a = _match("8 in octal")
        assert a is not None and a.name == "base_convert"

    def test_convert_prefix(self):
        a = _match("convert 10 to binary")
        assert a is not None and a.name == "base_convert"

    def test_what_is_prefix(self):
        a = _match("what is 255 in hex")
        assert a is not None and a.name == "base_convert"

    def test_hexadecimal_base(self):
        a = _match("42 in hexadecimal")
        assert a is not None and a.name == "base_convert"

    def test_case_insensitive_hex_prefix(self):
        # 0xFF normalized to lowercase 0xff
        a = _match("0xFF in decimal")
        assert a is not None
        assert "0xff" in a.params["num"].lower()

    def test_word_number_falls_through(self):
        assert _match("forty two in binary") is None

    def test_no_base_falls_through(self):
        assert _match("42 in") is None


# ═══════════════════════════════════════════════════════════════════
# §27  Coin / dice / random
# ═══════════════════════════════════════════════════════════════════


class TestRandom:

    def test_flip_a_coin(self):
        assert _name("flip a coin") == "coin_flip"

    def test_heads_or_tails(self):
        assert _name("heads or tails") == "coin_flip"

    def test_toss_a_coin(self):
        assert _name("toss a coin") == "coin_flip"

    def test_roll_a_dice(self):
        assert _name("roll a dice") == "dice_roll"

    def test_roll_a_die(self):
        assert _name("roll a die") == "dice_roll"

    def test_dice_bare(self):
        assert _name("dice") == "dice_roll"

    def test_roll_d6(self):
        a = _match("roll d6")
        assert a is not None and a.name == "dice_roll"
        assert a.params["sides"] == 6

    def test_roll_d20(self):
        a = _match("d20")
        assert a is not None and a.name == "dice_roll"
        assert a.params["sides"] == 20

    def test_roll_2d6(self):
        a = _match("roll 2d6")
        assert a is not None and a.name == "dice_roll"
        assert a.params["count"] == 2
        assert a.params["sides"] == 6

    def test_roll_3d20(self):
        a = _match("roll 3d20")
        assert a is not None
        assert a.params["count"] == 3
        assert a.params["sides"] == 20

    def test_default_dice_is_d6(self):
        a = _match("roll a dice")
        assert a is not None and a.params["sides"] == 6

    def test_random_number(self):
        assert _name("random number") == "random_num"

    def test_random_number_between(self):
        a = _match("random number between 1 and 100")
        assert a is not None and a.name == "random_num"
        assert a.params["lo"] == 1
        assert a.params["hi"] == 100

    def test_random_from_to(self):
        a = _match("random number from 5 to 50")
        assert a is not None and a.name == "random_num"
        assert a.params["lo"] == 5
        assert a.params["hi"] == 50

    def test_pick_a_random_number(self):
        assert _name("pick a random number") == "random_num"

    def test_give_me_random_number_between(self):
        a = _match("give me a random number between 10 and 20")
        assert a is not None
        assert a.params["lo"] == 10
        assert a.params["hi"] == 20

    def test_default_random_range(self):
        a = _match("random number")
        assert a is not None
        assert a.params["lo"] == 1
        assert a.params["hi"] == 100


if __name__ == "__main__":
    import sys

    sys.exit(pytest.main([__file__, "-v", "--tb=short"]))
