# QMD - Query Markup Documents

QMD is a local search engine for markdown documents that combines traditional keyword search with semantic vector search. Built as a TypeScript library and CLI tool, QMD indexes your markdown files into a SQLite database and exposes powerful search capabilities through multiple interfaces: a command-line tool, an MCP (Model Context Protocol) server for AI integration, and REST API endpoints. The search engine supports three query types - lexical (BM25), vector (semantic), and HyDE (hypothetical document embedding) - which can be combined for optimal recall.

The library organizes documents into collections with optional context annotations, making it ideal for knowledge bases, documentation sites, and personal note systems. QMD's architecture separates indexing from search, allowing you to embed documents once and query them instantly. The MCP server integration enables AI assistants like Claude to search and retrieve your local documents, while the REST API supports integration with any HTTP client.

## CLI Commands

### Adding Documents

```bash
# Add a single file
qmd add docs/readme.md

# Add files with glob pattern
qmd add "docs/**/*.md"

# Add to a named collection
qmd add --collection notes "journals/*.md"

# Add with custom context description
qmd add --context "API documentation for v2" docs/api/*.md
```

### Querying Documents

```bash
# Simple keyword search (auto-expands to lex/vec/hyde)
qmd query "authentication flow"

# Explicit lexical search with BM25
qmd query 'lex: "rate limiter" timeout -redis'

# Semantic vector search
qmd query 'vec: how does the API handle rate limiting'

# Combined multi-type query for best results
qmd query $'lex: auth token\nvec: how does authentication work'

# Search with intent disambiguation
qmd query --intent "web performance metrics" "performance"

# Filter by collection
qmd query --collection docs "error handling"

# Limit results and set minimum score
qmd query --limit 5 --min-score 0.5 "database migrations"
```

### Retrieving Documents

```bash
# Get document by path
qmd get docs/api/auth.md

# Get document by docid (from search results)
qmd get "#abc123"

# Get starting from specific line
qmd get "docs/large-file.md:100"

# Multi-get with glob pattern
qmd multi_get "journals/2025-05*.md"

# Multi-get with size limit
qmd multi_get --max-bytes 10240 "docs/*.md"
```

### Index Management

```bash
# Check index status
qmd status

# Generate embeddings for vector search
qmd embed

# Remove document from index
qmd rm docs/old-file.md

# Start MCP server (stdio transport)
qmd server

# Start HTTP server on specific port
qmd server --http --port 3000
```

## MCP Server Tools

### query - Search the Knowledge Base

```typescript
// MCP tool call structure
{
  "name": "query",
  "arguments": {
    "searches": [
      { "type": "lex", "query": "\"connection pool\" timeout" },
      { "type": "vec", "query": "why do database connections fail" },
      { "type": "hyde", "query": "Connection pool exhaustion occurs when all connections are in use..." }
    ],
    "limit": 10,
    "minScore": 0.3,
    "collections": ["docs"],
    "intent": "database performance troubleshooting",
    "rerank": true
  }
}

// Response structure
{
  "results": [
    {
      "docid": "#abc123",
      "file": "docs/database/connections.md",
      "title": "Connection Pool Management",
      "score": 0.87,
      "context": "Database documentation",
      "snippet": "1: # Connection Pooling\n2: \n3: When connections exceed the pool limit..."
    }
  ]
}
```

### get - Retrieve Single Document

```typescript
// MCP tool call
{
  "name": "get",
  "arguments": {
    "file": "docs/api/auth.md",  // or "#abc123" for docid
    "fromLine": 50,              // optional: start from line
    "maxLines": 100,             // optional: limit lines
    "lineNumbers": true          // optional: include line numbers
  }
}

// With line offset in path
{
  "name": "get",
  "arguments": {
    "file": "docs/api/auth.md:120"
  }
}
```

### multi_get - Batch Document Retrieval

```typescript
// MCP tool call
{
  "name": "multi_get",
  "arguments": {
    "pattern": "journals/2025-05*.md",  // glob or comma-separated list
    "maxLines": 50,
    "maxBytes": 10240,
    "lineNumbers": false
  }
}
```

### status - Index Health Check

```typescript
// MCP tool call
{
  "name": "status",
  "arguments": {}
}

// Response
{
  "totalDocuments": 150,
  "needsEmbedding": 5,
  "hasVectorIndex": true,
  "collections": [
    {
      "name": "docs",
      "path": "/home/user/docs",
      "pattern": "**/*.md",
      "documents": 120,
      "lastUpdated": "2025-01-15T10:30:00Z"
    }
  ]
}
```

## REST API Endpoints

### POST /query (or /search)

```bash
# Structured search request
curl -X POST http://localhost:3000/query \
  -H "Content-Type: application/json" \
  -d '{
    "searches": [
      { "type": "lex", "query": "authentication" },
      { "type": "vec", "query": "how to implement login" }
    ],
    "limit": 10,
    "minScore": 0.3,
    "intent": "user authentication flow"
  }'
```

### GET /health

```bash
curl http://localhost:3000/health
# Response: {"status":"ok","uptime":3600}
```

### POST /mcp (MCP Protocol)

```bash
# Initialize MCP session
curl -X POST http://localhost:3000/mcp \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","method":"initialize","id":1,"params":{"capabilities":{}}}'

# Call tool with session
curl -X POST http://localhost:3000/mcp \
  -H "Content-Type: application/json" \
  -H "mcp-session-id: <session-id>" \
  -d '{"jsonrpc":"2.0","method":"tools/call","id":2,"params":{"name":"query","arguments":{"searches":[{"type":"lex","query":"test"}]}}}'
```

## Query Syntax Reference

### Lexical (lex) Queries

```
# Prefix matching
lex: perf                    # matches "performance", "performant"

# Exact phrase
lex: "rate limiter"          # phrase must appear verbatim

# Exclusion
lex: performance -sports     # exclude documents with "sports"
lex: -"test data"            # exclude phrase

# Combined
lex: "machine learning" -"deep learning" tutorial
```

### Vector (vec) Queries

```
# Natural language questions
vec: how does the authentication system validate tokens
vec: what happens when a user exceeds their rate limit
```

### HyDE (hyde) Queries

```
# Write a hypothetical answer (50-100 words)
hyde: The rate limiter implements a token bucket algorithm where each user receives 100 tokens per minute. When a request arrives, one token is consumed. If no tokens remain, the request returns a 429 status code.
```

## Programmatic Usage

```typescript
import { createStore, getDefaultDbPath } from "@tobilu/qmd";

// Initialize store
const store = await createStore({
  dbPath: getDefaultDbPath(),
  configPath: "./qmd.config.json"  // optional
});

// Search documents
const results = await store.search({
  queries: [
    { type: "lex", query: "authentication" },
    { type: "vec", query: "how to implement secure login" }
  ],
  collections: ["docs"],
  limit: 10,
  minScore: 0.3,
  intent: "security best practices"
});

// Get single document
const doc = await store.get("docs/auth.md", { includeBody: true });
if (!("error" in doc)) {
  console.log(doc.title, doc.body);
}

// Multi-get with pattern
const { docs, errors } = await store.multiGet("journals/*.md", {
  includeBody: true,
  maxBytes: 10240
});

// Check index status
const status = await store.getStatus();
console.log(`${status.totalDocuments} documents indexed`);

// Clean up
await store.close();
```

## Summary

QMD excels in scenarios requiring fast, local search over markdown collections. For personal knowledge management, it indexes journals, notes, and documentation, enabling quick retrieval through natural language queries or precise keyword searches. Development teams can use QMD to create searchable documentation portals, combining it with the MCP server to let AI assistants answer questions directly from internal docs. The hybrid search approach (lex + vec + hyde) provides superior recall compared to keyword-only or embedding-only solutions.

Integration patterns vary by use case: CLI for shell scripts and automation, REST API for web applications, and MCP for AI-powered tools. The collection system supports multi-tenant setups where different document sets remain isolated yet searchable. Context annotations help disambiguate results when document titles overlap. For production deployments, run `qmd embed` after adding documents to enable vector search, and use the `--min-score` filter to exclude low-confidence matches. The SQLite backend ensures portability - your entire search index lives in a single file that can be backed up or shared.
