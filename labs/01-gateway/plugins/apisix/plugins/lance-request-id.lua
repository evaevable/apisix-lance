--
-- 学习用的自定义 APISIX 插件示例。
--
-- 它做两件事，用来演示两个不同阶段的能力边界：
--   1. rewrite 阶段：生成请求 ID，写入转发给上游的请求头；
--   2. header_filter 阶段：把同一个请求 ID 写入返回给客户端的响应头。
--
-- 注意：log 阶段已经无法再修改响应，所以响应头必须在 header_filter 设置。
-- 本插件面向本地实验，不是生产级实现。
--
local core = require("apisix.core")

local plugin_name = "lance-request-id"

local schema = {
    type = "object",
    properties = {
        header = {
            description = "name of the response header sent to the client",
            type = "string",
            default = "X-Lance-Request-Id",
        },
        upstream_header = {
            description = "name of the request header sent to the upstream",
            type = "string",
            default = "X-Lance-Plugin",
        },
        prefix = {
            description = "prefix of the generated request id",
            type = "string",
            default = "lance-",
        },
    },
}

local _M = {
    version = 0.1,
    priority = 1200,
    name = plugin_name,
    schema = schema,
}


function _M.check_schema(conf)
    return core.schema.check(schema, conf)
end


function _M.rewrite(conf, ctx)
    -- math.random 在 OpenResty 中由 LuaJIT 提供，配合时间戳足以区分演示请求。
    local request_id = conf.prefix .. ngx.now() .. "-" .. math.random(100000, 999999)

    -- 存进请求上下文，供后续阶段（这里是 header_filter）使用。
    ctx.lance_request_id = request_id

    -- 写入转发给上游的请求头。
    core.request.set_header(ctx, conf.upstream_header, request_id)

    core.log.info(plugin_name, " generated request id: ", request_id)
end


function _M.header_filter(conf, ctx)
    -- 响应头必须在这个阶段（或更早）设置；到 log 阶段就晚了。
    core.response.set_header(conf.header, ctx.lance_request_id or "unset")
end


return _M
