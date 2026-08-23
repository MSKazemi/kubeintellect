"""An edge that claims `source_kind='observation'` must be able to say *which* observation.

Every row in `kg_edges` carries `source_kind TEXT NOT NULL DEFAULT 'observation'`, and that
string is the sole input to `memory/security.trust_score`, where `'observation'` scores 1.0 —
the top of the table. Measured on the real ingest path before this change, every edge it wrote
carried that claim with `source_id = NULL`: the graph asserted a provenance class it could not
resolve. An operator asking *why does the agent believe this pod runs on that node* had
nowhere to go, and `evidence_ref` (memory property R4) had zero lines anywhere in v4.

The obvious fix is the wrong one. Giving the `Observation` an id would produce a non-null
`source_id` that still resolves to nothing, because observations are an in-memory stream with
no table behind them — the same defect in a new costume, and harder to spot. So the handle is
the apiserver's own: `uid` + `resourceVersion`, which names the exact object version the fact
came from and can be checked against the cluster.

The load-bearing test here is the end-to-end one. `observation_ref` alone is trivially
satisfiable by handing it a dict that already contains a uid; what actually breaks is the
*watcher* dropping the field, after which the ingest path silently returns to writing NULL.
So the chain is exercised from a raw `--output-watch-events` document all the way to the
captured INSERT arguments.
"""

from __future__ import annotations

import pytest
from app.memory import kg
from app.memory.security import trust_score
from app.sensorium.k8s_watcher import _pod_observation
from app.sensorium.observations import Observation

# Argument positions in the kg_edges INSERT: (cluster_id, src, rel, dst, attrs,
# source_kind, source_id, valid_from).
SOURCE_KIND, SOURCE_ID = 5, 6


class FakePool:
    """Captures edge inserts; every entity upsert returns a usable id."""

    def __init__(self) -> None:
        self.edges: list[tuple] = []

    async def execute(self, sql, *args):
        if "INSERT INTO kg_edges" in sql:
            self.edges.append(args)
        return "OK"

    async def fetchrow(self, sql, *args):
        if "INSERT INTO kg_entities" in sql:
            return {"id": f"ent-{args[1]}-{args[2]}"}
        return None                      # no pre-existing edge → always insert

    async def fetch(self, sql, *args):
        return []


@pytest.fixture
def pool():
    fake = FakePool()
    kg.init_kg(fake)
    yield fake
    kg.close_kg()


def watch_doc(*, uid: str = "3f2a-9c11", rv: str = "774102", node: str = "worker-3") -> dict:
    """One document as `kubectl get pods -A --watch --output-watch-events=true -o json`
    emits it — the actual input the watcher parses."""
    return {
        "type": "MODIFIED",
        "object": {
            "kind": "Pod",
            "metadata": {
                "name": "checkout-59d8",
                "namespace": "payments",
                "uid": uid,
                "resourceVersion": rv,
                "ownerReferences": [
                    {"kind": "ReplicaSet", "name": "checkout-59d8f", "controller": True}
                ],
            },
            "spec": {"nodeName": node},
            "status": {"phase": "Running"},
        },
    }


class TestTheChainFromWatchEventToStoredEdge:
    """The property that matters: a fact learned from the cluster cites the cluster."""

    async def test_every_edge_the_ingest_path_writes_cites_its_object_version(self, pool):
        obs = _pod_observation(watch_doc(), "cluster-a")
        assert obs is not None
        await kg.ingest_pod_observation(obs)

        assert len(pool.edges) == 2, "expected runs_on + owns"
        for args in pool.edges:
            assert args[SOURCE_KIND] == "observation"
            ref = args[SOURCE_ID]
            # Not merely non-null: it must contain the identity the apiserver gave us.
            assert ref is not None, "edge claims observation provenance with no source"
            assert "3f2a-9c11" in ref
            assert "774102" in ref

    async def test_both_edges_cite_the_same_observation(self, pool):
        """They were derived from one document; two different refs would be a lie."""
        await kg.ingest_pod_observation(_pod_observation(watch_doc(), "cluster-a"))

        refs = {args[SOURCE_ID] for args in pool.edges}
        assert len(refs) == 1

    async def test_the_ref_is_version_specific_not_just_object_specific(self, pool):
        """A name is not evidence. Two states of the same pod must cite different versions,
        or the ref cannot distinguish the observation that supported the fact from a later
        one that contradicts it."""
        await kg.ingest_pod_observation(
            _pod_observation(watch_doc(rv="774102", node="worker-3"), "cluster-a")
        )
        first = {args[SOURCE_ID] for args in pool.edges}
        pool.edges.clear()
        await kg.ingest_pod_observation(
            _pod_observation(watch_doc(rv="774999", node="worker-9"), "cluster-a")
        )
        second = {args[SOURCE_ID] for args in pool.edges}

        assert first and second and first.isdisjoint(second)

    async def test_no_edges_means_no_provenance_claim(self, pool):
        """A pod with neither node nor controller writes nothing — there is no fact to cite."""
        doc = watch_doc(node="")
        doc["object"]["metadata"]["ownerReferences"] = []
        await kg.ingest_pod_observation(_pod_observation(doc, "cluster-a"))

        assert pool.edges == []


class TestTheWatcherKeepsWhatTheRefNeeds:
    """The seam that silently reverts the fix: the watcher dropping the fields."""

    def test_the_observation_carries_the_apiserver_identity(self):
        obs = _pod_observation(watch_doc(), "cluster-a")
        assert obs is not None
        assert obs.fields["uid"] == "3f2a-9c11"
        assert obs.fields["resource_version"] == "774102"

    def test_the_existing_fields_are_untouched(self):
        """This change is additive; anything already reading these must not shift."""
        obs = _pod_observation(watch_doc(), "cluster-a")
        assert obs is not None
        assert obs.fields["node"] == "worker-3"
        assert obs.fields["owner"] == "ReplicaSet/checkout-59d8f"
        assert obs.fields["watch_type"] == "MODIFIED"


class TestObservationRefIsHonestAboutWhatItCannotDo:

    def test_no_uid_yields_no_ref_rather_than_a_fabricated_one(self):
        """A synthetic id would be non-null and point at nothing — worse than NULL, because
        it *looks* resolvable. Absent evidence is reported as absent."""
        obs = Observation(kind="pod_status", cluster_id="c", namespace="n", name="p",
                          fields={"status": "Running"})
        assert kg.observation_ref(obs) is None

    @pytest.mark.parametrize("uid", ["", "   ", None])
    def test_a_blank_uid_is_not_an_identity(self, uid):
        obs = Observation(kind="pod_status", cluster_id="c", namespace="n", name="p",
                          fields={"uid": uid, "resource_version": "12"})
        assert kg.observation_ref(obs) is None

    def test_a_uid_without_a_resource_version_still_names_the_object(self):
        obs = Observation(kind="pod_status", cluster_id="c", namespace="n", name="p",
                          fields={"uid": "abc", "resource_version": ""})
        assert kg.observation_ref(obs) == "pod_status:abc"

    async def test_an_observation_with_no_identity_still_writes_its_edges(self, pool):
        """Provenance is a claim about a fact, not a gate on learning it. Refusing the write
        would trade a weak citation for a missing topology — an unasked-for behaviour change.
        Whether such a row may keep `source_kind='observation'` is an open owner decision."""
        obs = Observation(
            kind="pod_status", cluster_id="c", namespace="n", name="p",
            fields={"status": "Running", "node": "w1", "owner": "Deployment/p"},
        )
        await kg.ingest_pod_observation(obs)

        assert len(pool.edges) == 2
        assert all(args[SOURCE_ID] is None for args in pool.edges)


class TestWhyThisIsNotCosmetic:

    def test_the_claim_the_edge_makes_is_the_top_of_the_trust_table(self):
        """`source_kind` is not decoration: it is the input to the memory write-admission
        trust score, and `observation` is maximal there. A claim that strong should be
        answerable."""
        assert trust_score("observation") == 1.0
        assert trust_score("observation") > trust_score("user_query")
