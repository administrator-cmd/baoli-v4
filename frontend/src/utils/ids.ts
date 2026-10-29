/**
 * 生成请求 id 与前端临时键，32 位十六进制，与后端 request_id 的 uuid4().hex 同形。
 *
 * 不用 crypto.randomUUID：它只在安全上下文（HTTPS 或 localhost）才有，
 * 通过 http://内网IP 访问时是 undefined；getRandomValues 没有这个限制。
 */
export function newId(): string {
  const bytes = crypto.getRandomValues(new Uint8Array(16));
  return Array.from(bytes, (byte) => byte.toString(16).padStart(2, "0")).join("");
}
