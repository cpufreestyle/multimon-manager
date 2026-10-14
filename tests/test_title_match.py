#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""标题宽松匹配的单测（复刻 Rectangle Pro 的 lenientMatch / TitleMatchingMode）。"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import title_match  # noqa: E402


class TestMatchStrength(unittest.TestCase):
    def test_exact_is_case_insensitive(self):
        mode, score = title_match.match_strength("ChatGPT", "chatgpt")
        self.assertEqual(mode, "exact")
        self.assertEqual(score, 100)

    def test_contained_when_prefix_unchanged(self):
        """标题加后缀（会话名）时仍能认出——这是最常见的标题变化。"""
        mode, score = title_match.match_strength(
            "ChatGPT", "ChatGPT — New Chat")
        self.assertEqual(mode, "contained")
        self.assertGreaterEqual(score, 50)
        self.assertLess(score, 100)

    def test_word_overlap_is_weakest(self):
        """实词交集非空即算，但分数必须低于 contained。"""
        mode, score = title_match.match_strength(
            "rea: Reverse Engineering Assistant", "rea decompiler skill")
        self.assertEqual(mode, "word")
        self.assertLess(score, 50)

    def test_unrelated_titles_do_not_match(self):
        self.assertEqual(
            title_match.match_strength("ChatGPT", "Qoder CN"), (None, 0))

    def test_empty_titles_do_not_match(self):
        self.assertEqual(title_match.match_strength("", "x"), (None, 0))
        self.assertEqual(title_match.match_strength("x", ""), (None, 0))

    def test_longer_contained_scores_higher(self):
        """覆盖比例越高越像：短后缀比长后缀更像原标题。"""
        _m1, s1 = title_match.match_strength("ChatGPT", "ChatGPT — x")
        _m2, s2 = title_match.match_strength(
            "ChatGPT", "ChatGPT — a very long conversation name here")
        self.assertGreater(s1, s2)


class TestBestCandidate(unittest.TestCase):
    CANDS = [("ChatGPT::ChatGPT", "ChatGPT"),
                 ("ChatGPT::ChatGPT — New Chat", "ChatGPT — New Chat"),
                 ("Qoder CN::Qoder CN", "Qoder CN")]

    def test_picks_exact_over_contained(self):
        h, mode, score = title_match.best_candidate("ChatGPT", self.CANDS)
        self.assertEqual(h, "ChatGPT::ChatGPT")
        self.assertEqual(mode, "exact")

    def test_picks_most_similar_when_no_exact(self):
        h, mode, _s = title_match.best_candidate(
            "ChatGPT — Old Chat", self.CANDS)
        self.assertIn("ChatGPT", h)
        self.assertIn(mode, ("contained", "word"))

    def test_same_app_multi_window_disambiguates(self):
        """同一 App 多个窗口：按标题认出保存的是哪一个，而不是第一个。"""
        cands = [("App::Win A", "Notes — A"),
                 ("App::Win B", "Notes — B"),
                 ("App::Win C", "Notes — C")]
        h, mode, _s = title_match.best_candidate("Notes — B", cands)
        self.assertEqual(h, "App::Win B")
        self.assertEqual(mode, "exact")

    def test_no_candidate_returns_none(self):
        h, mode, score = title_match.best_candidate(
            "Completely Different", self.CANDS)
        self.assertIsNone(h)
        self.assertIsNone(mode)
        self.assertEqual(score, 0)

    def test_single_char_words_ignored(self):
        """单字符词不参与交集，避免 "a" / "b" 之类的偶合。"""
        self.assertEqual(
            title_match.match_strength("a b c", "x y z"), (None, 0))


if __name__ == "__main__":
    unittest.main()
