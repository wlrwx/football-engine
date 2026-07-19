/**
 * Cloudflare Worker - 竞彩API代理
 * 
 * 解决 GitHub Actions 海外IP被腾讯云WAF拦截的问题。
 * Worker 部署在 Cloudflare 边缘节点，国内IP请求竞彩API，
 * GitHub Actions 请求 Worker，Worker 转发到竞彩官方。
 * 
 * 免费额度: 100,000 请求/天（每天2次预测×50场=100请求，绰绰有余）
 * 
 * 部署步骤:
 *   1. npm install -g wrangler
 *   2. wrangler login
 *   3. wrangler deploy
 * 
 * 使用方式（在 GitHub Actions 中）:
 *   SPORTTERY_PROXY=https://sporttery-proxy.<your-subdomain>.workers.dev
 *   python -m engine.main --date today
 *   
 *   代码中: 将 SPORTTERY_API 替换为 ${SPORTTERY_PROXY}/api/sporttery
 */

const SPORTTERY_UPSTREAM = 'https://webapi.sporttery.cn';

export default {
  async fetch(request, env, ctx) {
    const url = new URL(request.url);
    
    // CORS 预检
    if (request.method === 'OPTIONS') {
      return new Response(null, {
        headers: {
          'Access-Control-Allow-Origin': '*',
          'Access-Control-Allow-Methods': 'GET',
          'Access-Control-Allow-Headers': 'Content-Type',
        },
      });
    }

    // 健康检查
    if (url.pathname === '/health') {
      return Response.json({ status: 'ok', timestamp: Date.now() });
    }

    // 代理竞彩API: /api/sporttery/gateway/... → webapi.sporttery.cn/gateway/...
    if (url.pathname.startsWith('/api/sporttery/')) {
      const targetPath = url.pathname.replace('/api/sporttery', '');
      const targetUrl = `${SPORTTERY_UPSTREAM}${targetPath}${url.search}`;

      try {
        const upstreamResp = await fetch(targetUrl, {
          headers: {
            'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Referer': 'https://www.sporttery.cn/',
            'Accept': 'application/json',
          },
          // Cloudflare 默认用海外节点，但竞彩API不封Cloudflare IP
          // 如果仍被封，可加 cf: { resolveOverride: '...' } 强制走特定节点
        });

        const body = await upstreamResp.text();
        
        return new Response(body, {
          status: upstreamResp.status,
          headers: {
            'Content-Type': 'application/json; charset=utf-8',
            'Access-Control-Allow-Origin': '*',
            'Cache-Control': 'public, max-age=300',  // 缓存5分钟
            'X-Proxy': 'cf-worker',
          },
        });
      } catch (err) {
        return Response.json(
          { error: 'upstream_failed', detail: err.message },
          { status: 502 }
        );
      }
    }

    // 代理500万赔率（可选）: /api/500wan/... → odds.500.com/...
    if (url.pathname.startsWith('/api/500wan/')) {
      const targetPath = url.pathname.replace('/api/500wan', '');
      const targetUrl = `https://odds.500.com${targetPath}${url.search}`;

      try {
        const upstreamResp = await fetch(targetUrl, {
          headers: {
            'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Referer': 'https://odds.500.com/',
            'X-Requested-With': 'XMLHttpRequest',
          },
        });

        const body = await upstreamResp.text();
        return new Response(body, {
          status: upstreamResp.status,
          headers: {
            'Content-Type': 'application/json; charset=utf-8',
            'Access-Control-Allow-Origin': '*',
            'Cache-Control': 'public, max-age=120',
          },
        });
      } catch (err) {
        return Response.json(
          { error: 'upstream_failed', detail: err.message },
          { status: 502 }
        );
      }
    }

    return Response.json({ error: 'not_found' }, { status: 404 });
  },
};
