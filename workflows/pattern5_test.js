export const meta = {
  name: 'pattern5-simple-test',
  description: '测试工作流是否正常运行',
  phases: [
    { title: '测试', detail: '简单测试' },
  ],
}

phase('测试')
log('🔍 开始测试工作流...')

const result = await agent(
  `列出 tests/test_pattern5_concurrency.py 文件的前20行内容。`,
  { label: '测试Agent' }
)

log('✅ 测试完成')
return result || '测试成功'
