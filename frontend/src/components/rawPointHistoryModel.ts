import type { HistoryPoint, HistoryResponse, Tag } from '../api/client'

export const RAW_POINT_PAGE_SIZE = 200

type TagPage = {
  tags: Tag[]
  total_pages: number
}

type FetchTagPage = (
  nodeId: string,
  page: number,
  pageSize: number,
  search: undefined,
  dataType: undefined,
  tagType: string,
  readWrite: undefined,
  enabled: boolean,
  sortBy: string,
  sortOrder: 'asc',
) => Promise<TagPage>

export async function loadPhysicalNumericTags(
  nodeId: string,
  fetchPage: FetchTagPage,
): Promise<Tag[]> {
  const tags: Tag[] = []
  let page = 1
  let totalPages = 1
  do {
    const result = await fetchPage(
      nodeId, page, RAW_POINT_PAGE_SIZE, undefined, undefined, 'PHYSICAL',
      undefined, true, 'sort_order', 'asc',
    )
    tags.push(...result.tags)
    totalPages = Math.max(1, result.total_pages || 1)
    page += 1
  } while (page <= totalPages)
  return tags.filter((tag) => tag.data_type === 'FLOAT' || tag.data_type === 'INT')
}

export async function loadSelectedRawPointHistory(
  tagId: string | null,
  range: '1h' | '24h' | '7d',
  fetchHistory: (tagId: string, range: '1h' | '24h' | '7d') => Promise<HistoryResponse>,
): Promise<HistoryPoint[]> {
  if (!tagId) return []
  return (await fetchHistory(tagId, range)).points
}

export function requestResultIsCurrent({
  requestGeneration,
  currentGeneration,
  expectedNodeId,
  currentNodeId,
  resultNodeId,
}: {
  requestGeneration: number
  currentGeneration: number
  expectedNodeId: string
  currentNodeId: string
  resultNodeId?: string
}): boolean {
  return requestGeneration === currentGeneration
    && expectedNodeId === currentNodeId
    && (resultNodeId === undefined || resultNodeId === currentNodeId)
}
