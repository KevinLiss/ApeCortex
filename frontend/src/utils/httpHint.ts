/**
 * HTTP 错误可读化（M1）：把 axios 的英文/空洞错误转成可执行的中文提示。
 *
 * 背景：后端进程未更新时接口返回 404，用户此前只看到
 * "Request failed with status code 404"，无法自助定位。
 */
export function httpHint(error: any): string {
  const status = error?.response?.status as number | undefined
  const data = error?.response?.data
  const rawMsg =
    typeof data?.msg === 'string' ? data.msg : typeof data?.detail === 'string' ? data.detail : ''
  // FastAPI 默认 detail 是英文 "Not Found"，这类兜底文案不作为有效信息
  const backendMsg = ["Not Found", "Not found", "Bad Request", "Internal Server Error"].includes(rawMsg)
    ? ''
    : rawMsg

  if (status === 404) {
    return backendMsg || '接口不存在：后端可能未加载最新代码，请重启后端服务后再试'
  }
  if (status === 403) {
    return backendMsg || '权限不足：请联系管理员为你的角色分配相应权限'
  }
  if (status === 422) {
    return backendMsg || '请求参数不合法'
  }
  if (status && status >= 500) {
    return backendMsg || '服务端异常，请查看后端日志'
  }
  if (!status) {
    return '无法连接后端服务：请确认服务已启动、端口可访问'
  }
  return backendMsg || error?.message || '请求失败'
}