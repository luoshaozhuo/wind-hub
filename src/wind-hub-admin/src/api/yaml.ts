import { reactive } from 'vue'
import { api } from './client'

interface ConfigFile {name:string;exists:boolean;optional:boolean}
interface ConfigContent {name:string;content:string}

export const CONFIG_FILES:string[]=[]
export const yamlFiles=reactive<Record<string,string>>({})

export async function loadYamlFiles(){
  const files=await api<ConfigFile[]>('/config/files')
  CONFIG_FILES.splice(0,CONFIG_FILES.length,...files.filter(f=>f.exists||!f.optional).map(f=>f.name))
  const contents=await Promise.all(
    files.filter(f=>f.exists).map(f=>api<ConfigContent>('/config/files/'+encodeURIComponent(f.name))),
  )
  for(const row of contents)yamlFiles[row.name]=row.content
}
