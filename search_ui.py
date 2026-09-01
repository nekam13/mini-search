#!/usr/bin/env python3
"""
Mini Search - Vyhledávací rozhraní v4.0
Zjednodušená verze pro maximální spolehlivost
"""

from flask import Flask, render_template_string, request, jsonify
import crawler_engine

app = Flask(__name__)

# HTML Template pro vyhledávání
SEARCH_HTML = """
<!DOCTYPE html>
<html lang="cs">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Mini Search - Vyhledávání</title>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body { 
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            background: #1a1a2e; color: #e0e0e0; line-height: 1.6; padding: 20px;
        }
        .container { max-width: 1200px; margin: 0 auto; }
        h1 { color: #0f3460; margin-bottom: 10px; font-size: 2em; }
        h2 { color: #e94560; margin: 20px 0 10px; font-size: 1.3em; }
        .card { background: #16213e; border-radius: 10px; padding: 20px; margin-bottom: 20px; }
        .search-box { display: flex; gap: 10px; margin-bottom: 20px; }
        .search-box input { 
            flex: 1; padding: 15px; border-radius: 8px; border: 1px solid #333; 
            background: #1a1a2e; color: #e0e0e0; font-size: 1.1em;
        }
        .search-box button { 
            background: #e94560; color: white; border: none; padding: 15px 30px; 
            border-radius: 8px; cursor: pointer; font-size: 1.1em; font-weight: bold;
        }
        .search-box button:hover { background: #c81e45; }
        .result-item { 
            background: #16213e; border-radius: 8px; padding: 20px; margin-bottom: 15px;
            border-left: 4px solid #e94560;
        }
        .result-item:hover { background: #1f2b4a; }
        .result-title { color: #e94560; font-size: 1.2em; margin-bottom: 10px; }
        .result-url { color: #2196f3; font-size: 0.9em; margin-bottom: 10px; word-break: break-all; }
        .result-snippet { color: #aaa; line-height: 1.6; }
        .result-meta { color: #666; font-size: 0.85em; margin-top: 10px; }
        .no-results { text-align: center; color: #666; padding: 40px; }
        .stats { color: #666; font-size: 0.9em; margin-top: 20px; }
        .autocomplete-results { 
            position: absolute; background: #16213e; border-radius: 8px; 
            max-height: 300px; overflow-y: auto; z-index: 1000; width: calc(100% - 140px);
            box-shadow: 0 4px 20px rgba(0,0,0,0.3);
        }
        .autocomplete-item { padding: 12px 20px; cursor: pointer; }
        .autocomplete-item:hover { background: #1f2b4a; }
        .header-info { font-size: 0.9em; color: #666; margin-top: 5px; }
        .alert { padding: 15px; border-radius: 5px; margin-bottom: 20px; }
        .alert-warning { background: #ff980020; border-left: 4px solid #ff9800; color: #ff9800; }
        .alert-info { background: #2196f320; border-left: 4px solid #2196f3; color: #2196f3; }
    </style>
</head>
<body>
    <div class="container">
        <h1>🔍 Mini Search</h1>
        <div class="header-info">
            Vector backend: {{ vector_backend }} | 
            Port: 8095 | 
            <a href="http://localhost:8070" style="color: #2196f3;">Správcovská konzole →</a>
        </div>
        
        {% if vector_backend == 'dummy' %}
        <div class="alert alert-warning">
            ⚠️ Vector search není dostupný. Nainstalujte: <code>pip install torch --index-url https://download.pytorch.org/whl/cpu</code> a <code>pip install sentence-transformers==2.2.2</code>
        </div>
        {% endif %}
        
        <div class="card">
            <div style="position: relative;">
                <form id="searchForm" action="/" method="get">
                    <div class="search-box">
                        <input type="text" name="q" id="searchInput" placeholder="Zadejte vyhledávaný text..." 
                               value="{{ query }}" autocomplete="off" required>
                        <button type="submit">Vyhledat</button>
                    </div>
                </form>
                <div id="autocompleteResults" class="autocomplete-results" style="display: none;"></div>
            </div>
        </div>
        
        {% if query %}
        <div class="card">
            <h2>Výsledky pro: "{{ query }}"</h2>
            <div class="stats">Nalezeno: {{ total_results }} výsledků</div>
            
            {% if results %}
            <div id="resultsContainer">
                {% for result in results %}
                <div class="result-item">
                    <div class="result-title">{{ result.title or result.url }}</div>
                    <div class="result-url">{{ result.url }}</div>
                    <div class="result-snippet">{{ result.snippet or '...' }}</div>
                    <div class="result-meta">
                        {% if result.site_id %}Site ID: {{ result.site_id }}{% endif %}
                    </div>
                </div>
                {% endfor %}
            </div>
            {% else %}
            <div class="no-results">
                <p>🔍 Žádné výsledky nenalezeny</p>
                <p style="font-size: 0.9em; color: #666;">Zkuste jiné vyhledávací slovo nebo přidejte nové stránky přes správcovskou konzoli.</p>
            </div>
            {% endif %}
        </div>
        {% endif %}
        
        <div class="card" style="text-align: center; color: #666;">
            <p>Mini Search v4.0 | <a href="http://localhost:8070" style="color: #e94560;">Správcovská konzole</a></p>
        </div>
    </div>
    
    <script>
        let autocompleteTimeout;
        
        document.getElementById('searchInput').addEventListener('input', function(e) {
            clearTimeout(autocompleteTimeout);
            const query = e.target.value.trim();
            if (query.length < 2) {
                document.getElementById('autocompleteResults').style.display = 'none';
                return;
            }
            
            autocompleteTimeout = setTimeout(() => {
                fetch('/autocomplete?q=' + encodeURIComponent(query))
                    .then(response => response.json())
                    .then(data => {
                        const resultsDiv = document.getElementById('autocompleteResults');
                        if (data.results && data.results.length > 0) {
                            resultsDiv.innerHTML = data.results.map(r => 
                                '<div class="autocomplete-item" onclick="selectSuggestion(\'' + r + '\')">' + r + '</div>'
                            ).join('');
                            resultsDiv.style.display = 'block';
                        } else {
                            resultsDiv.style.display = 'none';
                        }
                    })
                    .catch(() => {
                        document.getElementById('autocompleteResults').style.display = 'none';
                    });
            }, 300);
        });
        
        function selectSuggestion(text) {
            document.getElementById('searchInput').value = text;
            document.getElementById('autocompleteResults').style.display = 'none';
            document.getElementById('searchForm').submit();
        }
        
        // Hide autocomplete when clicking outside
        document.addEventListener('click', function(e) {
            if (!e.target.closest('.search-box') && !e.target.closest('.autocomplete-results')) {
                document.getElementById('autocompleteResults').style.display = 'none';
            }
        });
    </script>
</body>
</html>
"""


def search(query, limit=50):
    """Search in indexed pages"""
    conn = crawler_engine.get_db()
    cursor = conn.cursor()
    
    # Simple full-text search
    search_term = f"%{query}%"
    cursor.execute(
        "SELECT id, site_id, url, title, content FROM pages WHERE title LIKE ? OR content LIKE ? LIMIT ?",
        (search_term, search_term, limit)
    )
    
    results = []
    for row in cursor.fetchall():
        results.append({
            'id': row[0],
            'site_id': row[1],
            'url': row[2],
            'title': row[3],
            'content': row[4],
            'snippet': row[4][:200] + '...' if len(row[4]) > 200 else row[4]
        })
    
    return results


@app.route('/')
def index():
    """Main search page"""
    query = request.args.get('q', '').strip()
    
    if query:
        results = search(query)
        return render_template_string(
            SEARCH_HTML,
            query=query,
            results=results,
            total_results=len(results),
            vector_backend=crawler_engine.VECTOR_BACKEND_TYPE
        )
    
    return render_template_string(
        SEARCH_HTML,
        query='',
        results=[],
        total_results=0,
        vector_backend=crawler_engine.VECTOR_BACKEND_TYPE
    )


@app.route('/autocomplete')
def autocomplete():
    """Autocomplete endpoint"""
    query = request.args.get('q', '').strip()
    
    if len(query) < 2:
        return jsonify({'results': []})
    
    try:
        conn = crawler_engine.get_db()
        cursor = conn.cursor()
        
        # Search in titles and URLs
        search_term = f"%{query}%"
        cursor.execute(
            "SELECT DISTINCT title FROM pages WHERE title LIKE ? LIMIT 10",
            (search_term,)
        )
        
        results = [row[0] for row in cursor.fetchall() if row[0]]
        
        return jsonify({'results': results[:10]})
    except Exception as e:
        return jsonify({'results': [], 'error': str(e)})


@app.route('/api/search')
def api_search():
    """API endpoint for search"""
    query = request.args.get('q', '').strip()
    limit = int(request.args.get('limit', 50))
    
    if not query:
        return jsonify({'results': [], 'query': '', 'total': 0})
    
    results = search(query, limit)
    return jsonify({
        'results': results,
        'query': query,
        'total': len(results)
    })


if __name__ == '__main__':
    print("=" * 70)
    print("Mini Search - Vyhledávací rozhraní v4.0")
    print("=" * 70)
    print(f"Vector backend: {crawler_engine.VECTOR_BACKEND_TYPE}")
    print()
    print("Spouštím na http://0.0.0.0:8095")
    print("Ctrl+C pro ukončení")
    print("=" * 70)
    
    app.run(host='0.0.0.0', port=8095, debug=False, threaded=True)
