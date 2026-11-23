"""Run the portable regression suite against this checkout's product sources."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

DESKTOP_CLASSES = {
    'ArchiveChoiceTests',
    'ConfirmationLayoutTests',
    'EasyJourneyTests',
    'EasyUITests',
    'EditorUpdateUITests',
    'InboxGuiCacheTests',
    'InboxGuiTests',
    'Interaction221Tests',
    'QuickstartTests',
    'RoundDialogTests',
    'WindowRecoveryTests',
}
DESKTOP_METHODS = {
    'test_advanced.AdvancedTests.test_gui_analyzes_child_and_saves_scoped_classification_for_restart',
    'test_advanced.AdvancedTests.test_gui_media_selection_persists_override_and_updates_row',
    'test_engine.EngineBatchAndGuiTests.test_background_analysis_button_uses_engine_and_shows_classification',
    'test_engine.EngineBatchAndGuiTests.test_selected_variant_rows_export_each_episode_only_once',
}
WINDOWS_METHODS = {
    'test_core.FilesTests.test_register_requires_editor_closed_and_backs_up_index',
    'test_manifest_sharing22.ManifestSharingTests.test_foreign_change_during_retry_is_never_overwritten',
    'test_manifest_sharing22.ManifestSharingTests.test_transient_windows_reader_does_not_abort_atomic_write',
    'test_series_publication236.SeriesPublicationTests.test_interrupted_move_recovers_from_original_sealed_receipt',
    'test_series_publication236.SeriesPublicationTests.test_report_changed_during_publication_is_not_resealed_with_a_new_hash',
    'test_series_publication236.SeriesPublicationTests.test_windows_extended_prefix_from_concurrent_parent_creation_is_same_path',
}


def fingerprints():
    paths = [ROOT / 'main.py']
    for directory in ('jyexport', 'native'):
        paths.extend(
            path for path in (ROOT / directory).rglob('*')
            if path.suffix in {'.py', '.cpp', '.h', '.bat'} or path.name == 'LICENSE'
        )
    return {
        path.relative_to(ROOT).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(paths) if path.is_file()
    }


def cases(suite):
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from cases(item)
        else:
            yield item


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--headless', action='store_true', help='Omit tests that open a Windows desktop window')
    parser.add_argument('--report', type=Path, help='Write a JSON report with the checked SHA256 values')
    args = parser.parse_args()

    sys.stdout.reconfigure(encoding='utf-8')
    sys.stderr.reconfigure(encoding='utf-8')
    from jyexport import __version__
    before = fingerprints()
    started = time.monotonic()

    # Resolve the system temporary directory once so macOS tests use one spelling.
    tempfile.tempdir = os.path.realpath(tempfile.gettempdir())
    skipped_for_platform = []
    with tempfile.TemporaryDirectory(prefix='jyexport-test-profile-') as profile:
        with patch.dict(os.environ, {'LOCALAPPDATA': profile}):
            discovered = unittest.defaultTestLoader.discover(str(ROOT / 'tests'), top_level_dir=str(ROOT))
            selected = unittest.TestSuite()
            for test in cases(discovered):
                name = test.id().removeprefix('tests.')
                desktop = type(test).__name__ in DESKTOP_CLASSES or name in DESKTOP_METHODS
                skip_desktop = desktop and (args.headless or os.name != 'nt')
                skip_windows = name in WINDOWS_METHODS and os.name != 'nt'
                if skip_desktop or skip_windows:
                    skipped_for_platform.append(name)
                else:
                    selected.addTest(test)
            result = unittest.TextTestRunner().run(selected)

    after = fingerprints()
    origins_ok = all(
        Path(module.__file__).resolve().is_relative_to(ROOT / 'jyexport')
        for name, module in sys.modules.items()
        if (name == 'jyexport' or name.startswith('jyexport.')) and getattr(module, '__file__', None)
    )
    report = {
        'version': __version__,
        'passed': result.wasSuccessful() and before == after and origins_ok,
        'tests': result.testsRun,
        'skipped': len(result.skipped),
        'platform_exclusions': skipped_for_platform,
        'seconds': round(time.monotonic() - started, 3),
        'source_unchanged': before == after,
        'all_product_imports_from_current_root': origins_ok,
        'failures': [(str(test), detail) for test, detail in result.failures],
        'errors': [(str(test), detail) for test, detail in result.errors],
        'source_files': after,
    }
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({
        'version': report['version'], 'passed': report['passed'], 'tests': report['tests'],
        'skipped': report['skipped'], 'platform_exclusions': len(skipped_for_platform),
    }, ensure_ascii=False))
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
