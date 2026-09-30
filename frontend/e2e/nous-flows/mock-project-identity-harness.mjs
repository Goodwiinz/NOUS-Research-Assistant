/** Opt-in same-origin auth proxy for research-project-identity.spec.ts. */
import { spawn } from 'node:child_process';
import http from 'node:http';

const user = {
  id: '33333333-3333-4333-8333-333333333333',
  email: 'browser@example.com',
  role: 'authenticated',
  aud: 'authenticated',
};
const token =
  'eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIzMzMzMzMzMy0zMzMzLTQzMzMtODMzMy0zMzMzMzMzMzMzMzMiLCJyb2xlIjoiYXV0aGVudGljYXRlZCIsImF1ZCI6ImF1dGhlbnRpY2F0ZWQiLCJleHAiOjQxMDI0NDQ4MDB9.sig';
const next = spawn('pnpm', ['exec', 'next', 'dev', '-p', '3042'], {
  stdio: 'inherit',
  env: {
    ...process.env,
    NEXT_PUBLIC_SUPABASE_URL: 'http://localhost:3040/supabase',
    NEXT_PUBLIC_SUPABASE_ANON_KEY: 'mock-anon',
    NEXT_PUBLIC_API_URL: 'http://localhost:3040/api/v1',
  },
});
const proxy = http.createServer((request, response) => {
  if (request.url?.startsWith('/supabase/auth/v1/')) {
    const body = request.url.includes('/token')
      ? { access_token: token, refresh_token: 'refresh', expires_in: 3600, expires_at: 4102444800, token_type: 'bearer', user }
      : { user };
    response.writeHead(200, { 'content-type': 'application/json' });
    response.end(JSON.stringify(body));
    return;
  }
  const upstream = http.request(
    { hostname: '127.0.0.1', port: 3042, path: request.url, method: request.method, headers: { ...request.headers, host: 'localhost:3040' } },
    (upstreamResponse) => {
      response.writeHead(upstreamResponse.statusCode ?? 500, upstreamResponse.headers);
      upstreamResponse.pipe(response);
    }
  );
  upstream.on('error', () => response.destroy());
  request.pipe(upstream);
});
proxy.on('upgrade', (request, socket, head) => {
  socket.on('error', () => {});
  const upstream = http.request({
    hostname: '127.0.0.1',
    port: 3042,
    path: request.url,
    headers: request.headers,
  });
  upstream.on('error', () => socket.destroy());
  upstream.on('upgrade', (response, upstreamSocket, upstreamHead) => {
    upstreamSocket.on('error', () => {});
    socket.write(
      `HTTP/1.1 101 Switching Protocols\r\n${Object.entries(response.headers)
        .map(([name, value]) => `${name}: ${value}`)
        .join('\r\n')}\r\n\r\n`
    );
    if (head.length) upstreamSocket.write(head);
    if (upstreamHead.length) socket.write(upstreamHead);
    upstreamSocket.pipe(socket);
    socket.pipe(upstreamSocket);
  });
  upstream.end();
});
proxy.listen(3040, () => console.log('Mock project identity harness: http://localhost:3040'));
/** @returns {void} */
const stop = () => {
  proxy.close();
  next.kill('SIGTERM');
};
process.on('SIGINT', stop);
process.on('SIGTERM', stop);
