"""Offline method, provenance and release-contract tests."""
import ast
import hashlib
import json
import math
from pathlib import Path
import re
import socket
import sys
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'src'))
from phase_space import clip, classify_checkpoint, classify_state, band_membership
from reproduce import compute, load_inputs, serialize, verify_input_manifest
from visuals import render_all


class PhaseMethodTests(unittest.TestCase):
    def test_inclusive_normalized_boundaries(self):
        self.assertEqual(classify_state(dict(B=.6,P=.35,S=.4,A=.35)), 'Hot')
        self.assertEqual(classify_state(dict(B=.2,P=.2,S=0,A=.25)), 'Cold')

    def test_hot_requires_every_coordinate(self):
        for key in ('B','P','S','A'):
            row=dict(B=1,P=1,S=1,A=1);row[key]=0
            self.assertNotEqual(classify_state(row), 'Hot')

    def test_missing_pressure_is_not_hot(self):
        row=dict(buyers=100,buy_sell_ratio='',top1_buy_share=.1,
                 volume_usd=10000,trade_count=200,average_trade_usd=120)
        result=classify_checkpoint(row)
        self.assertEqual(result['phase'],'Warm')
        self.assertTrue(result['missing_input'])
        self.assertIsNone(result['coordinates']['P'])

    def test_missing_structure_can_still_be_cold(self):
        row=dict(buyers=10,buy_sell_ratio=1.1,top1_buy_share=None,
                 volume_usd=500,trade_count=10,average_trade_usd=10)
        self.assertEqual(classify_checkpoint(row)['phase'],'Cold')

    def test_invalid_values_rejected(self):
        for value in (-1,float('inf')):
            with self.assertRaises(ValueError):classify_checkpoint({'buyers':value})
        with self.assertRaises(ValueError):classify_checkpoint({'top1_buy_share':1.1})

    def test_clipping_and_proposed_band(self):
        self.assertEqual(clip(-10),0)
        self.assertEqual(clip(10),1)
        self.assertTrue(math.isnan(clip(float('nan'))))
        knots=(10000,14000,22000,30000)
        for value,expected in [(10000,0),(12000,.5),(14000,1),(22000,1),(26000,.5),(30000,0)]:
            self.assertEqual(band_membership(value,*knots),expected)
        with self.assertRaises(ValueError):band_membership(1,2,1,3,4)


class PublicReproductionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):cls.a,cls.f,cls.p,cls.t=load_inputs()

    def test_expected_results_with_network_disabled(self):
        with patch.object(socket.socket,'connect',side_effect=AssertionError('Network forbidden')):
            actual=compute(self.a,self.f)
            self.assertEqual(serialize(actual),(ROOT/'data/expected_results.json').read_text())
        self.assertEqual(len(actual['synthetic_checkpoint_cases']),8)
        self.assertEqual(actual['historical_checkpoint_count_sum'],2100)
        self.assertEqual(actual['paper_distinct_id_arithmetic'],3359)
        self.assertAlmostEqual(actual['live_logged_usd_from_subtotals'],-166.9017)

    def test_corrupt_counts_fail(self):
        altered=json.loads(json.dumps(self.a));altered['warehouse_phase_rates'][0]['positives']+=1
        with self.assertRaises(ValueError):compute(altered,self.f)

    def test_cohort_rates_and_labels(self):
        jan=[r for r in self.a['warehouse_phase_rates'] if r['month']=='2025-01']
        self.assertEqual(sum(r['n'] for r in jan),32000)
        self.assertEqual(sum(r['positives'] for r in jan),583)
        for month in ('2025-01','2025-02'):
            rates={r['phase']:r['rate'] for r in self.a['warehouse_phase_rates'] if r['month']==month}
            self.assertLess(rates['Cold'],rates['Warm']);self.assertLess(rates['Warm'],rates['Hot'])

    def test_synthetic_size_and_volume_units_are_consistent(self):
        for row in self.f:
            self.assertAlmostEqual(float(row['volume_usd'])/float(row['trade_count']),
                                   float(row['average_trade_usd']))

    def test_manifest_fingerprints(self):verify_input_manifest()

    def test_all_figures_reproduce_and_parse(self):
        for name,svg in render_all(self.a,self.f,self.p,self.t).items():
            self.assertEqual(svg,(ROOT/'figures'/name).read_text())
            root=ET.fromstring(svg)
            self.assertEqual(root.tag,'{http://www.w3.org/2000/svg}svg')
            self.assertIn('aria-labelledby',root.attrib)

    def test_source_has_no_operational_imports(self):
        blocked={'requests','urllib','socket','subprocess','solders','solana','joblib','pickle','websockets'}
        for path in (ROOT/'src').glob('*.py'):
            for node in ast.walk(ast.parse(path.read_text())):
                if isinstance(node,ast.Import):names=[x.name.split('.')[0] for x in node.names]
                elif isinstance(node,ast.ImportFrom):names=[(node.module or '').split('.')[0]]
                else:continue
                self.assertFalse(blocked.intersection(names),path.name)


class EditorialContractTests(unittest.TestCase):
    def test_local_links_resolve(self):
        for path in ROOT.rglob('*.md'):
            for target in re.findall(r'\]\(([^)]+)\)',path.read_text()):
                if target.startswith(('http:','https:','#','mailto:')):continue
                destination=target.split('#',1)[0]
                if destination:self.assertTrue((path.parent/destination).exists(),f'{path.name}: {target}')

    def test_public_material_has_no_private_paths_or_obvious_credentials(self):
        # Assemble markers so the scanner does not match its own source literals.
        markers=[re.escape('/'+'Users'+'/'),re.escape('/'+'home'+'/'),
                 re.escape('sk-'+'proj-'),'BEGIN '+'[A-Z ]*PRIVATE'+' KEY',
                 re.escape('api-'+'key='),'https://[^\\s]*helius'+'-rpc']
        pattern=re.compile('|'.join(markers))
        for path in ROOT.rglob('*'):
            if '.git' in path.parts or '__pycache__' in path.parts or not path.is_file():continue
            if path.suffix not in ('.md','.py','.json','.csv','.svg','.txt','.toml'):continue
            self.assertIsNone(pattern.search(path.read_text()),path.name)

    def test_fixture_is_explicitly_synthetic(self):
        readme=(ROOT/'data/README.md').read_text()
        self.assertIn('synthetic',readme.lower())
        self.assertIn('not historical market observations',readme)


if __name__=='__main__':unittest.main()
