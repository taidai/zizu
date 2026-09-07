export function localDatabaseEnvironment(source, databaseName) {
  if (source.DB_HOST !== '127.0.0.1') throw new Error('纵向验收只允许本机数据库')
  const port = String(source.DB_PORT ?? '')
  if (!/^\d+$/.test(port) || Number(port) < 1 || Number(port) > 65535) throw new Error('请提供有效本机数据库端口 DB_PORT')
  if (!source.DB_USER?.trim() || !source.DB_PASSWORD) throw new Error('缺少本机数据库凭据 DB_USER / DB_PASSWORD')
  if (!/^zizu_task8_[0-9a-f]{32}_test$/.test(databaseName)) throw new Error('仅允许本轮新建的隔离测试数据库')
  return { DB_HOST: '127.0.0.1', DB_PORT: port, DB_USER: source.DB_USER, DB_PASSWORD: source.DB_PASSWORD, DB_NAME: databaseName }
}
