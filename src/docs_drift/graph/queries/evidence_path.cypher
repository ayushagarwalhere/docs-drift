// Reference query for consumers importing the exported evidence graph into Cypher.
// The built-in viewer runs locally in Cytoscape; no graph database is required.
MATCH path = (section:Section)-[:CONTAINS]->(claim:Claim)-[:REFERS_TO]->(symbol:Symbol)
             -[:DEFINED_IN]->(file:File)-[:CHANGED_BY]->(commit:Commit)
WHERE claim.id = $claim_id
RETURN path;
