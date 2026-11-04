import tempfile
import unittest
from pathlib import Path

from . import test_filter as cases


class Patch:
    def __init__(self):
        self.saved = []

    def setattr(self, obj, name, value):
        self.saved.append((obj, name, getattr(obj, name)))
        setattr(obj, name, value)

    def undo(self):
        for obj, name, value in reversed(self.saved):
            setattr(obj, name, value)


class TradeFeedRegressionTests(unittest.TestCase):
    def test_person_normalization(self):
        cases.test_person_normalization_is_full_name_and_removes_suffixes()

    def test_representation_identity(self):
        cases.test_representation_requires_same_person_and_agency()

    def test_headline_alone(self):
        cases.test_headline_alone_never_merges()

    def test_casting_identity(self):
        cases.test_casting_requires_same_person_and_project()

    def test_all_reports(self):
        cases.test_build_stories_keeps_every_report_including_same_outlet()

    def test_duplicate_url_and_time_window(self):
        cases.test_duplicate_url_is_one_report_and_time_window_is_respected()

    def test_archive_legacy_link(self):
        patch = Patch()
        try:
            with tempfile.TemporaryDirectory() as d:
                cases.test_archive_uses_same_policy_and_preserves_legacy_alternate(patch, Path(d))
        finally:
            patch.undo()

    def test_archive_earlier_lead(self):
        patch = Patch()
        try:
            with tempfile.TemporaryDirectory() as d:
                cases.test_archive_earlier_report_becomes_lead(patch, Path(d))
        finally:
            patch.undo()

    def test_renderers(self):
        cases.test_html_and_rss_render_all_alternate_links()


if __name__ == "__main__":
    unittest.main()

