/** 把未知异常转成可展示文案。 */

export function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}
