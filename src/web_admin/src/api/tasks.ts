// Tasks capability：任务定义/运行态查询与启停控制。
import { call, client } from './client'
import type { components } from './generated/schema'

export type TaskDto = components['schemas']['TaskResponse']
export type TaskPageDto = components['schemas']['TaskPageResponse']
export type TaskInstanceDto = components['schemas']['TaskInstanceResponse']

export function fetchTasks(pageSize = 200): Promise<TaskPageDto> {
  return call(client.GET('/api/v1/tasks', { params: { query: { page: 1, page_size: pageSize } } }))
}

export function fetchTask(taskId: string): Promise<TaskDto> {
  return call(client.GET('/api/v1/tasks/{task_id}', { params: { path: { task_id: taskId } } }))
}

export function fetchTaskInstances(taskId: string): Promise<TaskInstanceDto[]> {
  return call(
    client.GET('/api/v1/tasks/{task_id}/instances', { params: { path: { task_id: taskId } } }),
  )
}

export function startTask(taskId: string): Promise<TaskDto> {
  return call(
    client.POST('/api/v1/tasks/{task_id}/start', { params: { path: { task_id: taskId } } }),
  )
}

export function stopTask(taskId: string): Promise<TaskDto> {
  return call(
    client.POST('/api/v1/tasks/{task_id}/stop', { params: { path: { task_id: taskId } } }),
  )
}
