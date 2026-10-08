// Feature-map selection for nous-verify. The map lives at
// docs/engineering/feature-map.yaml and is validated in CI by
// scripts/ci/check_feature_map.py; this module only reads it to turn feature
// ids or changed paths into QA scenario ids.
import { readFile } from 'node:fs/promises';
import { parse } from 'yaml';

/**
 * fnmatch-compatible glob (Python `fnmatch.fnmatchcase`, which the CI checker
 * uses): `*` and `**` match any run of characters including `/`, `?` matches
 * one character, everything else is literal. Bracket classes are not used in
 * the map and are treated literally.
 */
export function globToRegExp(glob) {
  let source = '';
  for (const char of String(glob)) {
    if (char === '*') source += '.*';
    else if (char === '?') source += '.';
    else source += char.replace(/[.+^${}()|[\]\\]/g, '\\$&');
  }
  return new RegExp(`^${source}$`, 's');
}

export function parseFeatureMap(text) {
  const map = parse(text);
  if (!map || typeof map !== 'object' || map.version !== 1 || !Array.isArray(map.features)) {
    throw new Error('feature-map.yaml must have version 1 and a features list');
  }
  return map;
}

export async function loadFeatureMap(path) {
  return parseFeatureMap(await readFile(path, 'utf8'));
}

export function scenariosForFeatures(map, featureIds) {
  const wanted = new Set(featureIds);
  for (const id of wanted) {
    if (!map.features.some((feature) => feature.id === id)) throw new Error(`Unknown feature: ${id}`);
  }
  const ids = [];
  for (const feature of map.features) {
    if (!wanted.has(feature.id)) continue;
    for (const scenario of feature.scenarios ?? []) if (!ids.includes(scenario)) ids.push(scenario);
  }
  return ids;
}

export function featuresForChangedPaths(map, changedPaths) {
  const matched = [];
  for (const feature of map.features) {
    const patterns = (feature.owns ?? []).map(globToRegExp);
    if (changedPaths.some((path) => patterns.some((pattern) => pattern.test(path)))) matched.push(feature.id);
  }
  return matched;
}
