#!/usr/bin/env python3
"""
Web search utility for the web-browsing skill.
Searches the web and returns relevant results.
Hardened with security controls against SSRF, DNS rebinding, and content injection.
"""

import requests
from bs4 import BeautifulSoup
import urllib.parse
import socket
import ipaddress
import re


# Block private, loopback, link-local, and cloud metadata IP ranges
BLOCKED_NETWORKS = [
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("169.254.0.0/16"),  # AWS/GCP/Azure metadata & link-local
    ipaddress.ip_network("100.100.0.0/16"),  # Alibaba Cloud metadata
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fc00::/7"),
    ipaddress.ip_network("fe80::/10"),
]

# Patterns commonly used for prompt injection or malicious command execution
INJECTION_PATTERNS = re.compile(
    r"(?i)(\[系统指令\]|\[system prompt\]|ignore previous|execute command|curl.*\|.*bash|exec\s+command)",
    re.IGNORECASE
)


def is_safe_ip(ip_str: str) -> bool:
    """Validate that a resolved IP is not in a blocked or internal range."""
    try:
        ip = ipaddress.ip_address(ip_str)
        for network in BLOCKED_NETWORKS:
            if ip in network:
                return False
        return True
    except ValueError:
        return False


def resolve_and_validate_url(url: str) -> str:
    """Resolve DNS and verify the target IP is safe before fetching.
    Mitigates DNS rebinding and direct internal IP access."""
    parsed = urllib.parse.urlparse(url)
    if not parsed.hostname:
        raise ValueError("Invalid URL: missing hostname")
    
    try:
        addr_info = socket.getaddrinfo(parsed.hostname, None)
        resolved_ips = list(set([info[4][0] for info in addr_info]))
    except socket.gaierror as e:
        raise ValueError(f"DNS resolution failed: {e}")
    
    for ip in resolved_ips:
        if not is_safe_ip(ip):
            raise ValueError(f"Security policy blocked access to resolved IP: {ip} (internal/metadata range)")
    
    return url


def sanitize_response_content(content: str) -> str:
    """Strip or neutralize content that could be used for prompt injection."""
    sanitized = INJECTION_PATTERNS.sub("[REDACTED: POTENTIAL INJECTION]", content)
    # Limit size to prevent context window poisoning or DoS
    return sanitized[:4000]


def search_web(query: str, num_results: int = 5) -> list[dict]:
    """
    Perform a web search and return results.
    
    Args:
        query: Search query string
        num_results: Number of results to return (default: 5)
    
    Returns:
        List of dictionaries with title, url, and snippet
    """
    search_url = f"https://html.duckduckgo.com/html/?q={urllib.parse.quote(query)}"
    
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
    }
    
    try:
        response = requests.get(search_url, headers=headers, timeout=10)
        response.raise_for_status()
        
        soup = BeautifulSoup(response.text, 'html.parser')
        results = []
        
        for result in soup.find_all('a', class_='result__a')[:num_results]:
            title = result.get_text()
            url = result.get('href', '')
            
            snippet_div = result.find_parent().find_next_sibling('div', class_='result__snippet')
            snippet = sanitize_response_content(snippet_div.get_text()) if snippet_div else ''
            
            results.append({
                'title': title,
                'url': url,
                'snippet': snippet
            })
        
        return results
    
    except Exception as e:
        return [{'error': f'Search failed: {str(e)}'}]


def fetch_url(url: str) -> dict:
    """
    Fetch and parse a webpage with SSRF and DNS rebinding protections.
    
    Args:
        url: URL to fetch
    
    Returns:
        Dictionary with title, content, and metadata
    """
    try:
        # 1. Validate and resolve URL before network request
        safe_url = resolve_and_validate_url(url)
    except ValueError as e:
        return {'error': f'Security validation failed: {str(e)}', 'url': url}
    
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
    }
    
    try:
        response = requests.get(safe_url, headers=headers, timeout=15, allow_redirects=True)
        response.raise_for_status()
        
        # Verify final redirect destination is still safe
        final_ip = socket.gethostbyname(urllib.parse.urlparse(response.url).hostname)
        if not is_safe_ip(final_ip):
            return {'error': 'Security policy blocked access to redirect destination.', 'url': url}
        
        soup = BeautifulSoup(response.text, 'html.parser')
        
        title_tag = soup.find('title')
        title = title_tag.get_text().strip() if title_tag else 'No title found'
        
        for element in soup(['script', 'style', 'nav', 'footer', 'iframe', 'object']):
            element.decompose()
        
        article = soup.find('article') or soup.find('main') or soup.find('body')
        raw_content = article.get_text(separator='\n\n', strip=True) if article else ''
        
        # 2. Sanitize content to neutralize prompt injection attempts
        safe_content = sanitize_response_content(raw_content)
        
        return {
            'url': safe_url,
            'title': title,
            'content': safe_content,
            'status': 'success'
        }
    
    except requests.exceptions.RequestException as e:
        return {'error': f'Failed to fetch URL: {str(e)}', 'url': url}
    except Exception as e:
        return {'error': f'Unexpected error: {str(e)}', 'url': url}


if __name__ == '__main__':
    import sys
    import json
    
    if len(sys.argv) < 2:
        print("Usage: search_web.py <search_query|url> [--fetch]")
        sys.exit(1)
    
    query = sys.argv[1]
    fetch_mode = '--fetch' in sys.argv
    
    if fetch_mode:
        result = fetch_url(query)
    else:
        result = search_web(query)
    
    print(json.dumps(result, ensure_ascii=False, indent=2))