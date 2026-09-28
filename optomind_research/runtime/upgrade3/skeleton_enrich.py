# Fetch the authoritative per-node metadata the domain engine needs.
#
# The corpus deliberately stores only eight identity fields, and the provider
# citation counts lag badly for recent papers, so the domain engine is fed from
# here: title, abstract, year, citation count, publication types, and the
# embedding.
#
# Cold start used to enrich every paper any edge mentioned: 19k-32k nodes and
# 20-70 minutes per topic, which is what stopped the fourth cross-domain test
# from finishing.  The domain engine can only ever score the core plus the
# boundary that passes admission, so the set to fetch is bounded BEFORE the
# fetch, using signals the edges and the corpus already carry.  Nothing here
# decides relevance: it decides what is worth a provider call.

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import Any

import numpy as np

REPO = Path(__file__).resolve().parents[3]

#: Engineering safety cap on a single cold-start enrichment.  It exists so a
#: pathological graph cannot spend an unbounded number of provider calls; it is
#: NOT an admission rule, and a run that hits it says so in its summary.
DEFAULT_SAFETY_CAP = 12000


def prescreen(corpus: dict, edge_path: Path, config: dict | None = None) -> dict[str, Any]:
    """Cheap boundary pre-admission: which non-core papers could still be admitted.

    Uses only signals that exist before enrichment -- the citation edges and the
    snippet reference mentions in the corpus -- and returns a SUPERSET of the set
    the skeleton's admission step can keep, so no admitted paper is ever lost:

      core_papers_it_references >= min_core_papers_it_references      (edge)
      mentioned in a snippet reference mention                        (corpus)
      core_papers_referencing_it >= min_core_papers_referencing_it    (edge)

    The third clause is the skeleton's own rule minus the field-share test,
    because field_share = core_citers / max(total_citations, core_citers, 1) can
    only be below the floor for a paper that is cited more widely than by this
    core, and dropping such a paper here would drop a paper the skeleton could
    have admitted.
    """

    config = config or {}
    admit = ((config.get('boundary_admission') or {}) if isinstance(config, dict) else {})
    min_ref_by = int(admit.get('min_core_papers_referencing_it', 2))
    min_refs = int(admit.get('min_core_papers_it_references', 2))
    admit_mentions = bool(admit.get('admit_if_in_snippet_ref_mentions', True))

    core = {str(p["paper_id"]) for p in corpus.get("papers") or [] if p.get("paper_id")}
    core_citers: dict[str, set] = {}
    core_refs: dict[str, int] = {}
    mentions: set[str] = set()

    with Path(edge_path).open(encoding='utf-8') as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except ValueError:
                continue
            source = str(record.get("source_paper") or "")
            if source not in core:
                continue
            for node in record.get("references") or ():
                pid = str(node.get("paper_id") or "")
                if pid and pid not in core:
                    core_citers.setdefault(pid, set()).add(source)
            for node in record.get("citations") or ():
                pid = str(node.get("paper_id") or "")
                if pid and pid not in core:
                    core_refs[pid] = core_refs.get(pid, 0) + 1

    corpus_to_paper = {str(p['corpus_id']): str(p['paper_id'])
                       for p in corpus.get('papers') or []
                       if p.get('corpus_id') and p.get('paper_id')}
    # A snippet mention can name a boundary paper, so the corpus id map has to be
    # widened with the edge nodes exactly as the skeleton widens it.  Resolving a
    # mention only through the corpus dropped 281 admitted papers in X1.
    with Path(edge_path).open(encoding='utf-8') as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except ValueError:
                continue
            for relation in ('references', 'citations'):
                for node in record.get(relation) or ():
                    cid = str(node.get('corpus_id') or '')
                    pid = str(node.get('paper_id') or '')
                    if cid and pid:
                        corpus_to_paper.setdefault(cid, pid)

    for hit in corpus.get('retrieval_hits') or []:
        for mention in ((hit.get('snippet_locator') or {}).get('ref_mentions') or []):
            cid = mention.get('matched_paper_corpus_id')
            if cid not in (None, ''):
                mentions.add(str(cid))

    mention_ids = {corpus_to_paper.get(cid, 'corpus:' + cid) for cid in mentions}

    selected: list[str] = []
    reasons: dict[str, list[str]] = {}
    candidates = set(core_citers) | set(core_refs) | mention_ids
    for pid in candidates:
        if not pid or pid in core or pid.startswith('corpus:'):
            continue
        why = []
        if core_refs.get(pid, 0) >= min_refs:
            why.append('references_core')
        if admit_mentions and pid in mention_ids:
            why.append('mentioned_in_snippet')
        if len(core_citers.get(pid) or ()) >= min_ref_by:
            why.append('cited_by_core')
        if why:
            reasons[pid] = why
            selected.append(pid)
    return {
        'core': sorted(core),
        'selected': sorted(selected),
        'reasons': reasons,
        'candidates': len(candidates),
        'core_citers': {pid: len(v) for pid, v in core_citers.items()},
        'core_refs': core_refs,
        'snippet_mentions': len(mention_ids),
        'thresholds': {'min_core_papers_referencing_it': min_ref_by,
                       'min_core_papers_it_references': min_refs,
                       'admit_if_in_snippet_ref_mentions': admit_mentions},
    }

def _load_existing(out_dir: Path) -> dict:
    path = Path(out_dir) / 'NODE_META.json'
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except ValueError:
        return {}


def _write_embeddings(out_dir: Path, vectors: dict) -> int:
    order = sorted(vectors)
    matrix = (np.asarray([vectors[p] for p in order], dtype=np.float32)
              if order else np.zeros((0, 0), np.float32))
    np.savez_compressed(Path(out_dir) / 'NODE_EMBEDDINGS.npz',
                        ids=np.asarray(order, dtype=object), matrix=matrix)
    return int(matrix.shape[1]) if matrix.size else 0


def enrich(corpus_path: Path, edge_path: Path, out_dir: Path, batch: int = 500,
           *, prescreen_boundary: bool = True, safety_cap: int | None = DEFAULT_SAFETY_CAP,
           checkpoint_every: int = 4, config: dict | None = None) -> dict[str, Any]:
    from optomind_research.s2_intelligence_gateway import S2IntelligenceGateway

    started = time.monotonic()
    out_dir = Path(out_dir)
    corpus = json.loads(Path(corpus_path).read_text(encoding='utf-8'))
    core = {str(p['paper_id']) for p in corpus['papers'] if p.get('paper_id')}
    corpus_to_paper = {str(p['corpus_id']): str(p['paper_id'])
                       for p in corpus['papers'] if p.get('corpus_id') and p.get('paper_id')}

    ids: list[str] = sorted(core)
    seen = set(ids)
    prescreen_info: dict[str, Any] = {'mode': 'all_edge_nodes'}

    # A previous skeleton names the exact node set the domain engine scored, so a
    # rerun of a topic reuses it instead of re-deriving the boundary.
    admitted_path = out_dir / 'SCHOLARLY_SKELETON.json'
    if admitted_path.exists():
        previous = json.loads(admitted_path.read_text(encoding='utf-8'))
        support = (previous.get('pools') or {}).get('graph_support_pool') or []
        if not support:
            support = [row.get('paper_id') for row in previous.get('boundary_papers') or ()]
        for pid in support:
            pid = str(pid or '')
            if pid and pid not in seen:
                seen.add(pid)
                ids.append(pid)
        prescreen_info = {'mode': 'previous_skeleton', 'support': len(support)}
        print('restricted to core + admitted boundary:', len(ids), flush=True)
    elif prescreen_boundary:
        screen = prescreen(corpus, Path(edge_path), config)
        for pid in screen['selected']:
            if pid not in seen:
                seen.add(pid)
                ids.append(pid)
        prescreen_info = {
            'mode': 'cheap_pre_admission',
            'candidates': screen['candidates'],
            'selected': len(screen['selected']),
            'snippet_mentions': screen['snippet_mentions'],
            'thresholds': screen['thresholds'],
        }
        print('cheap pre-admission: %d candidates -> %d selected, core %d' % (
            screen['candidates'], len(screen['selected']), len(core)), flush=True)
    else:
        with Path(edge_path).open(encoding='utf-8') as handle:
            for line in handle:
                try:
                    record = json.loads(line)
                except ValueError:
                    continue
                for relation in ('references', 'citations'):
                    for node in record.get(relation) or ():
                        pid = str(node.get('paper_id') or '')
                        cid = str(node.get('corpus_id') or '')
                        if cid and pid:
                            corpus_to_paper.setdefault(cid, pid)
                        if pid and pid not in seen:
                            seen.add(pid)
                            ids.append(pid)

    selected_before_cap = len(ids)
    truncated = False
    if safety_cap and len(ids) > int(safety_cap):
        prescreen_info['safety_cap'] = int(safety_cap)
        prescreen_info['selected_before_cap'] = selected_before_cap
        truncated = True
        ids = ids[:int(safety_cap)]
        print('WARNING safety cap hit: enriching %d of %d nodes; the run is marked '
              'truncated' % (len(ids), selected_before_cap), flush=True)

    # Resume: anything already in NODE_META.json was paid for once already.
    existing = _load_existing(out_dir)
    meta: dict[str, dict[str, Any]] = {pid: dict(row) for pid, row in existing.items()}
    vectors: dict[str, list[float]] = {}
    vectors_path = out_dir / 'NODE_EMBEDDINGS.npz'
    if vectors_path.exists() and existing:
        try:
            data = np.load(vectors_path, allow_pickle=True)
            for index, pid in enumerate(str(x) for x in data['ids']):
                vectors[pid] = [float(v) for v in data['matrix'][index]]
        except Exception:  # noqa: BLE001
            vectors = {}
    todo = [pid for pid in ids if pid not in meta]
    if todo and len(todo) != len(ids):
        print('resuming; already collected: %d' % (len(ids) - len(todo)), flush=True)

    print('nodes to enrich:', len(ids), '(todo %d)' % len(todo), flush=True)
    gateway = S2IntelligenceGateway()
    out_dir.mkdir(parents=True, exist_ok=True)

    def checkpoint() -> None:
        (out_dir / 'NODE_META.json').write_text(json.dumps(meta, ensure_ascii=False),
                                               encoding='utf-8')
        (out_dir / 'CORPUS_ID_MAP.json').write_text(json.dumps(corpus_to_paper, ensure_ascii=False),
                                                   encoding='utf-8')
        _write_embeddings(out_dir, vectors)

    total = len(todo)
    done = 0
    for start in range(0, total, batch):
        chunk = todo[start:start + batch]
        try:
            records, _ = gateway.batch_papers(chunk)
        except Exception as exc:  # noqa: BLE001
            print('  batch failed:', type(exc).__name__, flush=True)
            continue
        for record in records:
            pid = str(getattr(record, 'paper_id', '') or '')
            if not pid:
                continue
            meta[pid] = {
                'paper_id': pid,
                'title': getattr(record, 'title', '') or '',
                'abstract': getattr(record, 'abstract', '') or '',
                'year': getattr(record, 'year', None),
                'citation_count': int(getattr(record, 'citation_count', 0) or 0),
                'influential_citation_count': int(
                    getattr(record, 'influential_citation_count', 0) or 0),
                'venue': getattr(record, 'venue', '') or '',
                'authors': list(getattr(record, 'authors', []) or []),
                'doi': getattr(record, 'doi', '') or '',
                'corpus_id': str(getattr(record, 'corpus_id', '') or ''),
                'publication_types': list(getattr(record, 'publication_types', []) or []),
                'is_core': pid in core,
            }
            vector = list(getattr(record, 'specter2_vector', []) or [])
            if vector:
                vectors[pid] = [float(v) for v in vector]
        done = min(start + batch, total)
        elapsed = max(1e-6, time.monotonic() - started)
        rate = done / elapsed * 60.0
        remaining = (total - done) / max(rate, 1e-6)
        print('  %d/%d  meta=%d vectors=%d  %.0f/min  ~%.0f min left' % (
            done, total, len(meta), len(vectors), rate, remaining), flush=True)
        if checkpoint_every and (start // batch + 1) % int(checkpoint_every) == 0:
            checkpoint()

    checkpoint()
    vector_dim = int(len(next(iter(vectors.values()))) if vectors else 0)
    summary = {
        'nodes': len(ids),
        'meta': len(meta),
        'vectors': len(vectors),
        'vector_dim': vector_dim,
        'prescreen': prescreen_info,
        'truncated': truncated,
        'selected_before_cap': selected_before_cap,
        'resumed_from_previous_meta': len(ids) - total,
        'elapsed_seconds': round(time.monotonic() - started, 1),
    }
    (out_dir / 'ENRICH_SUMMARY.json').write_text(json.dumps(summary, ensure_ascii=False, indent=1),
                                                encoding='utf-8')
    print(json.dumps(summary, ensure_ascii=False), flush=True)
    return summary


def load_meta(path: Path) -> dict[str, dict[str, Any]]:
    return json.loads(Path(path).read_text(encoding='utf-8'))


def load_vectors(path: Path):
    data = np.load(Path(path), allow_pickle=True)
    ids = [str(x) for x in data['ids']]
    matrix = data['matrix']
    return {pid: matrix[index] for index, pid in enumerate(ids)}, matrix, ids


def main() -> int:
    import argparse

    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except Exception:  # noqa: BLE001
        pass
    parser = argparse.ArgumentParser()
    parser.add_argument('--corpus', default='outputs/upgrade3/QP02/CANDIDATE_CORPUS.json')
    parser.add_argument('--edges', default='outputs/upgrade3/SKELETON/GRAPH_EDGES.jsonl')
    parser.add_argument('--out', default='outputs/upgrade3/SKELETON')
    parser.add_argument('--safety-cap', type=int, default=DEFAULT_SAFETY_CAP)
    parser.add_argument('--no-prescreen', action='store_true')
    args = parser.parse_args()
    enrich(REPO / args.corpus, REPO / args.edges, REPO / args.out,
           prescreen_boundary=not args.no_prescreen, safety_cap=args.safety_cap)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
