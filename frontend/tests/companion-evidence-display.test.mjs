import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import test from 'node:test';
import ts from 'typescript';

const source = await readFile(new URL('../src/companionEvidenceDisplay.ts', import.meta.url), 'utf8');
const code = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 } }).outputText;
const display = await import(`data:text/javascript;base64,${Buffer.from(code).toString('base64')}`);

test('unavailable prices never become an orphan currency symbol or zero', () => {
  for (const value of [undefined, null, '', ' ', 'NaN', 'Infinity', {}, '$14.85']) assert.equal(display.evidenceMoney(value), '—');
  assert.equal(display.evidenceMoney('14.85'), '$14.85');
  assert.equal(display.evidenceMoney('2,778.43'), '$2,778.43');
  assert.equal(display.evidenceMoney('0.00'), '$0.00');
  assert.equal(display.evidenceMoney(undefined, true), 'Not applicable');
});

test('selection axes are labeled and the product ID is not an unlabeled price axis', () => {
  const selection = { product_id: '6383', width: 24, height: 18, colorspec: '4/4', quantity: 6, flute_direction: 'vertical', stakes_per_sign: 1 };
  assert.equal(display.evidenceSelection(selection), 'Width 24 × Height 18 · Printing 4/4 · Qty 6 · Flute direction vertical · H-stakes per sign 1');
  assert.equal(display.evidenceSelection({}), '—');
});

test('a blocked orientation keeps its observed price and the matching block result distinct', () => {
  const item = { selection: {}, expected: 'blocked_orientation', match: true,
    observed: { button_disabled: true, total_text: '$18.54', warning_text: 'Landscape sign required' } };
  assert.equal(display.evidenceExpectedResult(item), 'Blocked orientation');
  assert.equal(display.evidenceMoney(item.expected_retail, true), 'Not applicable');
  assert.equal(display.evidenceDisplayed(item), 'Blocked · shown $18.54');
  assert.equal(display.evidenceCaseResult(item), 'Match · blocked as expected');
  assert.equal(display.evidenceCaseResult({ ...item, match: false }), 'Mismatch');
  assert.equal(display.evidenceCaseResult({ ...item, match: undefined }), 'Unknown');
  assert.equal(display.evidenceCaseResult({ ...item, observed: { button_disabled: false } }), 'Match');
});

test('normal display matches remain explicit without inventing a stored supplier cost', () => {
  const item = { selection: {}, expected: 'price', expected_retail: '14.85', displayed_retail: '14.85', match: true };
  assert.equal(display.evidenceExpectedResult(item), 'Price');
  assert.equal(display.evidenceDisplayed(item), '$14.85');
  assert.equal(display.evidenceCaseResult(item), 'Match');
  assert.equal(display.evidenceMoney(item.site_cost), '—');
});
