import { SidebarTrigger } from "@buildingai/ui/components/ui/sidebar";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@buildingai/ui/components/ui/tabs";
import { useParams } from "react-router-dom";

import Annotation from "./annotation";
import Messages from "./messages";

export default function Logs() {
  const { id } = useParams();
  const agentId = id ?? "";

  return (
    <Tabs defaultValue="messages" className="flex h-full min-h-0 flex-col gap-0">
      <div className="flex flex-col gap-2 px-4 py-3 md:flex-row md:items-center md:gap-4 md:px-6 md:py-4">
        <div className="flex items-center gap-2 md:gap-4">
          <SidebarTrigger className="md:hidden" />
          <h1 className="text-lg font-semibold">日志与标注</h1>
        </div>
        <TabsList className="max-w-full shrink-0 overflow-x-auto">
          <TabsTrigger value="messages">对话记录</TabsTrigger>
          <TabsTrigger value="annotation">标注管理</TabsTrigger>
        </TabsList>
      </div>
      <div className="min-h-0 flex-1 px-4 pb-2 md:px-6">
        <TabsContent value="messages" className="mt-0 h-full">
          <Messages agentId={agentId} />
        </TabsContent>
        <TabsContent value="annotation" className="mt-0 h-full">
          <Annotation agentId={agentId} />
        </TabsContent>
      </div>
    </Tabs>
  );
}
