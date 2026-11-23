// MIT License (c) 2026 wenshui330
#include "jy_draftc.h"

namespace jydc {
namespace {

enum : size_t {
    O_SVC = 0x08, O_API = 0x28, O_TID = 0x48, O_INKIND = 0x58, O_PAYLOAD = 0x80, O_BOOLS = 0x90,
    O_RTID = 0x48, O_RCODE = 0x4C, O_OUT = 0x58, O_CFG = 0x78, O_TAIL = 0x460,
    O_CW = 0x47, O_CH = 0x4B, O_CHARD = 0x4F, O_CFPS = 0x52, O_CCODEC = 0x5A,
    O_CQUAL = 0x62, O_CBR = 0x66, O_CCOLOR = 0x74, O_CMP4 = 0x281, O_REQSZ = 0x98
};

template <class T> T read_at(const void *point) {
    T value;
    std::memcpy(&value, point, sizeof(value));
    return value;
}
template <class T> void write_at(void *point, T value) {
    std::memcpy(point, &value, sizeof(value));
}

struct Pair final {
    void *object;
    void *control;
};

struct Str32 final {
    union {
        char local[16];
        char *remote;
    } data;
    size_t size;
    size_t capacity;

    void set(const char *text, size_t length, void *(*allocate)(size_t) = nullptr) {
        size = length;
        if (length < 16) {
            std::memset(data.local, 0, 16);
            std::memcpy(data.local, text, length);
            capacity = 15;
        } else {
            char *block = allocate ? static_cast<char *>(allocate(length + 1))
                                   : static_cast<char *>(std::malloc(length + 1));
            std::memcpy(block, text, length);
            block[length] = 0;
            data.remote = block;
            capacity = length;
        }
    }
    void set(const std::string &text, void *(*allocate)(size_t) = nullptr) {
        set(text.data(), text.size(), allocate);
    }
    const char *c_str() const { return capacity < 16 ? data.local : data.remote; }
};

using FnNew = void *(*)(size_t);
using FnInstance = void *(*)();
using FnStartup = void (*)(void *, const void *, const void *);
using FnOpenSession = std::int32_t (*)(void *, const void *, const void *);
using FnPumpOnce = bool (*)(void *);
using FnGetSession = void (*)(void *, void *, std::int32_t);
using FnInvoke = void (*)(void *, void *, void *, std::int32_t);
using FnInvokeSync = void (*)(void *, void *, const void *, std::int32_t);
using FnVeWrapper = void (*)(void *, void *);
using FnOpenTimelineEditor = void (*)(void *, std::int32_t, std::uint8_t);
using FnDraftTransaction = void (*)(void *, void *, const void *, std::int32_t);
using FnProjectClientInit = void (*)(void *, void *, std::int32_t);
using FnDeserialize = void (*)(void *, const void *);
using FnDraftFromJson = void (*)(void *, const void *);
using FnDraftToJson = void (*)(void *, const void *);
using FnGetLogger = void *(*)();
using FnSetAlogFunction = void (*)(void *, void *, void *);
using FnSetLogLevel = void (*)(void *, const void *);
using FnSetSubtitleCanvasVertical = void (*)(double);
using FnRestoreDraft = void (*)(std::int32_t, std::uint8_t, std::int32_t);
using FnExportFactory = void (*)(void *);

void __cdecl stub_a(void *) {}
void __cdecl stub_b(void *) {}
void *g_fake_vtable[3] = {nullptr, reinterpret_cast<void *>(&stub_a), reinterpret_cast<void *>(&stub_b)};

struct EngineSignals final {
    std::atomic<bool> restored{false};
    std::atomic<bool> finished{false};
    std::atomic<bool> failed{false};
    std::atomic<long long> log_calls{0};
    std::mutex mutex;
    std::string last_error;
};

EngineSignals g_signals;

bool mentions(const char *text, const char *needle) {
    return text && std::strstr(text, needle) != nullptr;
}
bool mentions_any(const char *needle, const char *a, const char *b, const char *c, const char *d) {
    return mentions(a, needle) || mentions(b, needle) || mentions(c, needle) || mentions(d, needle);
}

void __cdecl engine_log(const char *file, const char *function, int line, const char *tag, int level,
                        const char *format, ...) {
    g_signals.log_calls.fetch_add(1, std::memory_order_relaxed);
    char body[1024]{};
    if (format && *format && std::strlen(format) < 512) {
        va_list args;
        va_start(args, format);
        std::vsnprintf(body, sizeof(body), format, args);
        va_end(args);
    }
    const char *const text = body;
    if ((mentions(function, "driverRun") || mentions(text, "driverRun")) &&
        (mentions(file, "draft_service.cpp") || mentions(text, "restoreDraft"))) {
        g_signals.restored.store(true);
    }
    if (mentions_any("VE_INFO_COMPILE_DONE", text, format, tag, function)) g_signals.finished.store(true);
    if (mentions_any("VE_ERROR_COMPILE", text, format, tag, function)) {
        g_signals.failed.store(true);
        char rendered[1024]{};
        std::snprintf(rendered, sizeof(rendered), "%s:%d | L%d | %s | %s | %s", file ? file : "", line, level,
                      function ? function : "", tag ? tag : "", text);
        std::lock_guard<std::mutex> lock(g_signals.mutex);
        g_signals.last_error = rendered;
    }
    if (g_verbose) {
        char rendered[2048]{};
        std::snprintf(rendered, sizeof(rendered), "[ve] %s:%d | L%d | %s | %s | %s", file ? file : "", line,
                      level, function ? function : "", tag ? tag : "", text);
        debug_line(rendered);
    }
}

std::int32_t reply_code(const void *response) {
    return response ? read_at<std::int32_t>(static_cast<const unsigned char *>(response) + O_RCODE) : -999;
}
std::int32_t reply_task(const void *response) {
    return response ? read_at<std::int32_t>(static_cast<const unsigned char *>(response) + O_RTID) : -1;
}

class ExportEngine final {
public:
    explicit ExportEngine(Module &module) : module_(module) {}

    void load() {
        instance_ = module_.require<FnInstance>(name::kServerInstance, "instance");
        startup_ = module_.require<FnStartup>(name::kServerStartup, "startup");
        open_session_ = module_.require<FnOpenSession>(name::kOpenSession, "openSession");
        pump_once_ = module_.require<FnPumpOnce>(name::kPumpOnce, "pumpOnce");
        get_session_ = module_.require<FnGetSession>(name::kGetSession, "getSession");
        invoke_ = module_.require<FnInvoke>(name::kInvoke, "invoke");
        invoke_sync_ = module_.require<FnInvokeSync>(name::kInvokeSync, "invokeSync");
        ve_wrapper_ = module_.require<FnVeWrapper>(name::kGetVeWrapper, "getVeWrapper");
        open_timeline_ = module_.require<FnOpenTimelineEditor>(name::kOpenTimelineEditor, "openTimelineEditor");
        draft_transaction_ =
            module_.require<FnDraftTransaction>(name::kDraftTransaction, "draftTransaction");
        project_init_ = module_.require<FnProjectClientInit>(name::kProjectClientInit, "ProjectClient::init");
        deserialize_ =
            module_.require<FnDeserialize>(name::kDeserializePersistentDraft, "deserialize_persistent_draft");
        draft_from_json_ = module_.require<FnDraftFromJson>(name::kGetDraftFromJson, "GetDraftFromJson");
        get_logger_ = module_.require<FnGetLogger>(name::kGetLogger, "getLogger");
        set_alog_ = module_.require<FnSetAlogFunction>(name::kSetAlogFunction, "setAlogFunction");
        set_log_level_ = module_.require<FnSetLogLevel>(name::kSetLogLevel, "setLogLevel");
        set_subtitle_canvas_vertical_ = module_.require<FnSetSubtitleCanvasVertical>(
            "?setLyraSubtitleDefaultFontsizeCanvasVertical@lvve@@YAXN@Z",
            "setLyraSubtitleDefaultFontsizeCanvasVertical");
        module_.optional(draft_to_json_, name::kGetJsonFromDraft);

        operator_new_ = static_cast<FnNew>(module_.at(kAbi.operator_new));
        restore_draft_ = static_cast<FnRestoreDraft>(module_.at(kAbi.restore_draft));
        export_factory_ = static_cast<FnExportFactory>(module_.at(kAbi.export_factory));
        if (!module_.owns_code(reinterpret_cast<const void *>(operator_new_)) ||
            !module_.owns_code(reinterpret_cast<const void *>(restore_draft_)) ||
            !module_.owns_code(reinterpret_cast<const void *>(export_factory_))) {
            fail(EX_ENTRY, "rva-bound entry outside videoeditor.dll: abi " + std::string(kAbi.version) +
                               " does not match this dll");
        }
        g_engine_loaded = true;
    }

    void pump(int count) {
        while (count-- > 0) pump_once_(server_);
    }

    Pair request(bool owned, const char *service, const char *api, bool real_vtable, size_t payload_size = 0) {
        const size_t total = 0x10 + (payload_size ? payload_size : O_REQSZ);
        auto *control = owned ? static_cast<unsigned char *>(operator_new_(total))
                              : static_cast<unsigned char *>(std::calloc(1, total));
        if (!control) fail(EX_EXPORT, "engine allocation failed (request control block)");
        std::memset(control, 0, total);
        auto *object = control + 0x10;
        void *control_vtable = real_vtable ? module_.at(kAbi.vt_control)
                                           : static_cast<void *>(g_fake_vtable + 1);
        write_at<std::uint64_t>(control, reinterpret_cast<std::uint64_t>(control_vtable));
        write_at<std::uint32_t>(control + 8, 1);
        write_at<std::uint32_t>(control + 0xC, 1);
        if (real_vtable) {
            write_at<std::uint64_t>(object, reinterpret_cast<std::uint64_t>(module_.at(kAbi.vt_request)));
        }
        reinterpret_cast<Str32 *>(object + O_SVC)->set(service);
        reinterpret_cast<Str32 *>(object + O_API)->set(api);
        return Pair{object, control};
    }

    void *server() const noexcept { return server_; }
    void set_server(void *server) noexcept { server_ = server; }
    std::int32_t session() const noexcept { return session_; }
    void set_session(std::int32_t session) noexcept { session_ = session; }
    FnInstance only_instance() const noexcept { return instance_; }
    FnStartup startup() const noexcept { return startup_; }
    FnOpenSession open_session() const noexcept { return open_session_; }
    FnPumpOnce pump_once() const noexcept { return pump_once_; }
    FnGetSession get_session() const noexcept { return get_session_; }
    FnInvoke invoke() const noexcept { return invoke_; }
    FnInvokeSync invoke_sync() const noexcept { return invoke_sync_; }
    FnVeWrapper ve_wrapper() const noexcept { return ve_wrapper_; }
    FnOpenTimelineEditor open_timeline_editor() const noexcept { return open_timeline_; }
    FnDraftTransaction draft_transaction() const noexcept { return draft_transaction_; }
    FnProjectClientInit project_client_init() const noexcept { return project_init_; }
    FnDeserialize deserializer() const noexcept { return deserialize_; }
    FnDraftFromJson draft_from_json() const noexcept { return draft_from_json_; }
    FnDraftToJson draft_to_json() const noexcept { return draft_to_json_; }
    FnGetLogger get_logger() const noexcept { return get_logger_; }
    FnSetAlogFunction set_alog_function() const noexcept { return set_alog_; }
    FnSetLogLevel set_log_level() const noexcept { return set_log_level_; }
    FnSetSubtitleCanvasVertical set_subtitle_canvas_vertical() const noexcept { return set_subtitle_canvas_vertical_; }
    FnRestoreDraft restore_draft() const noexcept { return restore_draft_; }
    FnExportFactory export_factory() const noexcept { return export_factory_; }
    FnNew operator_new() const noexcept { return operator_new_; }

private:
    Module &module_;
    void *server_ = nullptr;
    std::int32_t session_ = 0;
    FnNew operator_new_ = nullptr;
    FnInstance instance_ = nullptr;
    FnStartup startup_ = nullptr;
    FnOpenSession open_session_ = nullptr;
    FnPumpOnce pump_once_ = nullptr;
    FnGetSession get_session_ = nullptr;
    FnInvoke invoke_ = nullptr;
    FnInvokeSync invoke_sync_ = nullptr;
    FnVeWrapper ve_wrapper_ = nullptr;
    FnOpenTimelineEditor open_timeline_ = nullptr;
    FnDraftTransaction draft_transaction_ = nullptr;
    FnProjectClientInit project_init_ = nullptr;
    FnDeserialize deserialize_ = nullptr;
    FnDraftFromJson draft_from_json_ = nullptr;
    FnDraftToJson draft_to_json_ = nullptr;
    FnGetLogger get_logger_ = nullptr;
    FnSetAlogFunction set_alog_ = nullptr;
    FnSetLogLevel set_log_level_ = nullptr;
    FnSetSubtitleCanvasVertical set_subtitle_canvas_vertical_ = nullptr;
    FnRestoreDraft restore_draft_ = nullptr;
    FnExportFactory export_factory_ = nullptr;
};

// Verified only for the exact kAbi/DLL fingerprint. Native-owned configuration
// remains alive for the export process; no editor process or settings are touched.
fs::path verified_effect_mask_resources(const fs::path &runtime) {
    const fs::path root = runtime / L"Resources" / L"lumi_js_resources";
    const auto reject_link = [](const fs::path &path) {
        const DWORD attr = ::GetFileAttributesW(path.c_str());
        if (attr == INVALID_FILE_ATTRIBUTES || (attr & FILE_ATTRIBUTE_REPARSE_POINT))
            fail(EX_EXPORT, "missing or linked effect-mask resource: " + path_text(path));
    };
    reject_link(runtime); reject_link(runtime / L"Resources"); reject_link(root);
    struct Resource { const char *relative; std::uintmax_t bytes; const char *digest; };
    static constexpr std::array<Resource, 63> expected{{
        {"config.json", 985, "1BF7F1D016AFE3253D3F6A5EC7DD8710B935B3F28DFC14FB3AAE65216408A6AA"},
        {"js/feature/feature.js", 21074, "3E393A293B05DE2681C634EC3B35361C3B96F402CE43098DA743D5AE6A84013D"},
        {"resources/featureBlend/AmazingFeature_blend/content.json", 154, "365E3911C46AE10C6600F297F7410817DC371C4C0302992E35A7B7EE1FC0DF76"},
        {"resources/featureBlend/AmazingFeature_blend/ImageBusinessSlider.json", 35, "B408C5A3949A0A77BACBC934E82CED44E25DB3491C54A891BE064158592810CF"},
        {"resources/featureBlend/AmazingFeature_blend/js-meta.json", 2, "4F53CDA18C2BAA0C0354BB5F9A3ECBE5ED12AB4D8E11BA873C2F11161202B945"},
        {"resources/featureBlend/AmazingFeature_blend/lua/ImageBusinessSlider.lua", 777, "C85339BA94EC16C19CC47D1CA31D755AFEC392C81723B122D1AE2EEA53FFA1BF"},
        {"resources/featureBlend/AmazingFeature_blend/lua/SeekModeScript.lua", 1169, "0FE69042CF76358287A3C94449C5E248387340ABF70B7C7CB20D254DBFF31E22"},
        {"resources/featureBlend/AmazingFeature_blend/lua-meta.json", 2, "4F53CDA18C2BAA0C0354BB5F9A3ECBE5ED12AB4D8E11BA873C2F11161202B945"},
        {"resources/featureBlend/AmazingFeature_blend/LuaRTTI.MarkGen.lua", 0, "E3B0C44298FC1C149AFBF4C8996FB92427AE41E4649B934CA495991B7852B855"},
        {"resources/featureBlend/AmazingFeature_blend/main.scene", 2942, "8E223AC9104BF9C7B5DF20866B3B1CEAE940E15BA3B71E3943AAF9528CFD4AC2"},
        {"resources/featureBlend/AmazingFeature_blend/material/blend.material", 833, "7F39828AF852EB47EB35923B406EC8971377F748DF1B75853FBB8AEA28DCD7BB"},
        {"resources/featureBlend/AmazingFeature_blend/mesh/Quad.mesh", 1279, "A2B0E2C65A2E1C3C9713E2FC0E7F9D5738B91BD4E2DEAA4DAE2988FB32FED6AF"},
        {"resources/featureBlend/AmazingFeature_blend/rt/NewScreenRT.rt", 586, "8D0D8A4F81CDC84563565B8BAF46778404365033B1EF28338F54C2A1C67B3243"},
        {"resources/featureBlend/AmazingFeature_blend/rt/outputTex.rt", 530, "0941CE398B7BEBEB33156638F94584A7681B5B837184745009D01D44F2188263"},
        {"resources/featureBlend/AmazingFeature_blend/scene.config", 344, "B704F62770775A071DC0FDB03AE5D0F674F939F605B3AE83B3C8E42ECA2619F9"},
        {"resources/featureBlend/AmazingFeature_blend/sticker.config", 301, "EC9734C99C86848FB99511ECD9369019DEDE6591F018EB112711CE745BC36BF6"},
        {"resources/featureBlend/AmazingFeature_blend/xshader/blend.frag", 823, "3C86F88E215714E2C04731A4CDC650FB05450A0BBBDCCA6AB994901B16ADC092"},
        {"resources/featureBlend/AmazingFeature_blend/xshader/blend.vert", 245, "FB3E9E7A966147CC5CB48B1A087931BFBDEA744A0D0E0DEF0F07070BA48A73CC"},
        {"resources/featureBlend/AmazingFeature_blend/xshader/blend.xshader", 1349, "139F1289D96F569E9D9A0AE1097E12ED2CD42ABC3DE63B8C3BFBA3859A1D2A69"},
        {"resources/featureBlend/config.json", 371, "7306474B9B48787E53A9533DF500AA9B3F464B621FD1080E0D995583AC1048AE"},
        {"resources/featureBlit/AmazingFeature_blit/algorithmConfig.json", 387, "1DE8BE7AF15F6D70B848B76FCFE4D4BD46A8895472B6C48C28A4AB89036641A4"},
        {"resources/featureBlit/AmazingFeature_blit/content.json", 153, "1DA6C492766A404600977306AAB4EC3BF09298DA69EADD638A25547073B499EF"},
        {"resources/featureBlit/AmazingFeature_blit/ImageBusinessSlider.json", 35, "B408C5A3949A0A77BACBC934E82CED44E25DB3491C54A891BE064158592810CF"},
        {"resources/featureBlit/AmazingFeature_blit/js-meta.json", 2, "4F53CDA18C2BAA0C0354BB5F9A3ECBE5ED12AB4D8E11BA873C2F11161202B945"},
        {"resources/featureBlit/AmazingFeature_blit/lua/ImageBusinessSlider.lua", 777, "885B7E669399C4DD4D6D226842AAE0355F5B5589081B0D6F461C7199590E3E1D"},
        {"resources/featureBlit/AmazingFeature_blit/lua-meta.json", 2, "4F53CDA18C2BAA0C0354BB5F9A3ECBE5ED12AB4D8E11BA873C2F11161202B945"},
        {"resources/featureBlit/AmazingFeature_blit/LuaRTTI.MarkGen.lua", 0, "E3B0C44298FC1C149AFBF4C8996FB92427AE41E4649B934CA495991B7852B855"},
        {"resources/featureBlit/AmazingFeature_blit/main.scene", 2275, "75631DEE29EB987719A31F5C33AFA851E8C739188FB3AFC75C38E4E8D1679068"},
        {"resources/featureBlit/AmazingFeature_blit/mesh/Quad.mesh", 1279, "34B175A62D42DDDAF1695BF8D19572B3C0447C4E1CBCB01D3372CB4553D92A61"},
        {"resources/featureBlit/AmazingFeature_blit/rt/NewScreenRT.rt", 586, "AB3C4FB6DACA999A4F02B57803FE48DC067AB825CD36F1BFE0695D69E88B41C6"},
        {"resources/featureBlit/AmazingFeature_blit/rt/outputTex.rt", 527, "20B0B57A2648C1B3E6C3B3F0480AE9E1175C34AF6C5D8C108FFD1950F2A5313A"},
        {"resources/featureBlit/AmazingFeature_blit/sticker.config", 276, "F883DAF39BD812B9CE40A457046F29AFB5100D178C2E242EEA662D3142FE4BAE"},
        {"resources/featureBlit/AmazingFeature_blit/xshader/noface.xshader", 1347, "7C170325D2B9B0B5AAE1FDD050AB405735891DF14845156F56627BF067F35606"},
        {"resources/featureBlit/config.json", 374, "E2C5F71A03C5E84D76279BAF9E4F9F02ED53B5D92A26928063DC90FF94138E51"},
        {"resources/featureMask/AmazingFeature_mask/content.json", 152, "A7DF9B9AAD6263225470B43A2DA742884EDB597A03B6F29D3BF07F2ACDF899DA"},
        {"resources/featureMask/AmazingFeature_mask/ImageBusinessSlider.json", 35, "B408C5A3949A0A77BACBC934E82CED44E25DB3491C54A891BE064158592810CF"},
        {"resources/featureMask/AmazingFeature_mask/js-meta.json", 2, "4F53CDA18C2BAA0C0354BB5F9A3ECBE5ED12AB4D8E11BA873C2F11161202B945"},
        {"resources/featureMask/AmazingFeature_mask/lua/ImageBusinessSlider.lua", 777, "C85339BA94EC16C19CC47D1CA31D755AFEC392C81723B122D1AE2EEA53FFA1BF"},
        {"resources/featureMask/AmazingFeature_mask/lua/SeekModeScript.lua", 1169, "0FE69042CF76358287A3C94449C5E248387340ABF70B7C7CB20D254DBFF31E22"},
        {"resources/featureMask/AmazingFeature_mask/lua-meta.json", 2, "4F53CDA18C2BAA0C0354BB5F9A3ECBE5ED12AB4D8E11BA873C2F11161202B945"},
        {"resources/featureMask/AmazingFeature_mask/LuaRTTI.MarkGen.lua", 0, "E3B0C44298FC1C149AFBF4C8996FB92427AE41E4649B934CA495991B7852B855"},
        {"resources/featureMask/AmazingFeature_mask/main.scene", 3661, "1F67A509A7DD47E3CFD38D98E75335CCD4003F2F401080B23B065E2ABAD8A263"},
        {"resources/featureMask/AmazingFeature_mask/material/blend.material", 833, "7F39828AF852EB47EB35923B406EC8971377F748DF1B75853FBB8AEA28DCD7BB"},
        {"resources/featureMask/AmazingFeature_mask/mesh/Quad.mesh", 1279, "A2B0E2C65A2E1C3C9713E2FC0E7F9D5738B91BD4E2DEAA4DAE2988FB32FED6AF"},
        {"resources/featureMask/AmazingFeature_mask/rt/NewScreenRT.rt", 586, "8D0D8A4F81CDC84563565B8BAF46778404365033B1EF28338F54C2A1C67B3243"},
        {"resources/featureMask/AmazingFeature_mask/rt/outputTex.rt", 530, "0941CE398B7BEBEB33156638F94584A7681B5B837184745009D01D44F2188263"},
        {"resources/featureMask/AmazingFeature_mask/scene.config", 344, "B704F62770775A071DC0FDB03AE5D0F674F939F605B3AE83B3C8E42ECA2619F9"},
        {"resources/featureMask/AmazingFeature_mask/sticker.config", 301, "EC9734C99C86848FB99511ECD9369019DEDE6591F018EB112711CE745BC36BF6"},
        {"resources/featureMask/AmazingFeature_mask/xshader/blend.frag", 820, "B673787F86A72CD150C93A3822A8855CE3B8661395C010A202BA97FF5D4DA91A"},
        {"resources/featureMask/AmazingFeature_mask/xshader/blend.vert", 245, "FB3E9E7A966147CC5CB48B1A087931BFBDEA744A0D0E0DEF0F07070BA48A73CC"},
        {"resources/featureMask/AmazingFeature_mask/xshader/blend.xshader", 1349, "139F1289D96F569E9D9A0AE1097E12ED2CD42ABC3DE63B8C3BFBA3859A1D2A69"},
        {"resources/featureMask/config.json", 370, "5185FF893403848CCB1D6EF8F8DF322139ED98BA0BECE4C7752BD70E152B4CD5"},
        {"resources/lumi_hub/config.json", 64, "5E57049062D13F77B9C68520164888A45A13C3C8CCD9BEC96449EB3D31F914BA"},
        {"resources/lumi_hub/content.json", 228, "C03ED58CC8FCA2F4910FE65A98E4B6C209D7285E79B2068D40C3B7DA30298966"},
        {"resources/lumi_hub/effects/LumiHub/LumiExtra.frag", 144, "F5CE442C91816B0672F108D392357DF34AC744DA80704F8F2680AAE1C53D797C"},
        {"resources/lumi_hub/effects/LumiHub/LumiExtra.material", 639, "AF5D16647E261CBC68ADF886E54AF37CF218D5979FAF29A07CAF05492FDBA5DD"},
        {"resources/lumi_hub/effects/LumiHub/LumiExtra.mesh", 1255, "D41B120719EC1D210E48EFC7C3B373B457895EB632061FC1B1E00D50888B3532"},
        {"resources/lumi_hub/effects/LumiHub/LumiExtra.vert", 297, "F9EEFC2B4AA4EE0A5030C77917C0A3FD2ACBCBAFC332C00E7569D3AD90DE22F1"},
        {"resources/lumi_hub/effects/LumiHub/LumiExtra.xshader", 1731, "7D5E1280B567FE83C6E2B389159331C6EEEE134DBB0F9F665BA6A495B67D2FEF"},
        {"resources/lumi_hub/lua/LumiFamily/LumiHub.lua", 39931, "ED62606FBB4173504334D15601D06E51B2512F468B0450805124D05C357234F5"},
        {"resources/lumi_hub/lua/LumiFamily/LumiObjectExtension.lua", 16542, "729AB739A7AD74AEC41A4290903B10B9CC96D50CC317AA03D80ED560B57F49E7"},
        {"resources/lumi_hub/lua/LumiFamily/LumiUtils.lua", 8271, "E0B561C4059B478A5B9BED8B69A01F3DCC382623720322019FE5E9EA8CFB7D70"},
        {"resources/lumi_hub/prefabs/LumiRoot.prefab", 700, "C871A2FAB271CBC0E2DD6B2141FB141C8353871ABC4A6AEAE25EA5CCEFE1FE75"},
    }};
    size_t count = 0;
    for (const auto &entry : fs::recursive_directory_iterator(root)) {
        reject_link(entry.path());
        if (entry.is_regular_file()) ++count;
    }
    if (count != expected.size()) fail(EX_EXPORT, "effect-mask resource inventory mismatch");
    for (const auto &item : expected) {
        const fs::path path = root / fs::path(to_wide(item.relative));
        if (!fs::is_regular_file(path) || fs::file_size(path) != item.bytes ||
                hex_upper(sha256_file(path)) != item.digest)
            fail(EX_EXPORT, "effect-mask resource fingerprint mismatch: " + path_text(path));
    }
    return fs::canonical(root);
}

void initialize_effect_masks(Module &module, void *wrapper, const fs::path &runtime) {
    const fs::path resources = verified_effect_mask_resources(runtime);
    using GetConfig = void (*)(void *, Pair *);
    auto get_config = module.require<GetConfig>(
        "?getVeAdapterConfig@VeWrapper@wrapper@lyra@@QEAA?AV?$shared_ptr@UVEAdapterConfig@adapter@lvve@@@std@@XZ",
        "getVeAdapterConfig");
    Pair config{};
    get_config(wrapper, &config);
    if (!config.object) fail(EX_EXPORT, "effect-mask native session config is missing");
    // Effect-mask script root in 11.6.0.14552 VEAdapterConfig. Controlled
    // circle/rectangle/rotation/animated-position exports bind this exact field.
    constexpr size_t offset = 0x998;
    auto *field = static_cast<unsigned char *>(config.object) + offset;
    MEMORY_BASIC_INFORMATION memory{};
    if (::VirtualQuery(field, &memory, sizeof(memory)) != sizeof(memory) ||
            memory.State != MEM_COMMIT || !(memory.Protect & PAGE_READWRITE) ||
            field + sizeof(std::string) > static_cast<unsigned char *>(memory.BaseAddress) + memory.RegionSize)
        fail(EX_EXPORT, "effect-mask config layout is not writable native storage");
    static_assert(sizeof(std::string) == 0x20, "effect-mask string ABI changed");
    auto &script_root = *reinterpret_cast<std::string *>(field);
    if (!script_root.empty() || script_root.capacity() != 15)
        fail(EX_EXPORT, "unexpected native effect-mask script-root default");
    script_root = path_text(resources);
    if (script_root != path_text(resources)) fail(EX_EXPORT, "effect-mask script-root readback failed");
    note_line("effect-mask-runtime: initialized tree=07214b002e53deef19c97b7a6c74f128b3f99212073828acb966d85b26716bd6");
}

int patch_placeholders(std::string &json, const fs::path &directory) {
    std::string dir = path_text(directory);
    for (char &c : dir) {
        if (c == '\\') c = '/';
    }
    constexpr const char *kKey = "##_draftpath_placeholder_";
    std::string out;
    out.reserve(json.size());
    size_t position = 0;
    int count = 0;
    for (;;) {
        const size_t begin = json.find(kKey, position);
        if (begin == std::string::npos) break;
        const size_t end = json.find("_##", begin);
        if (end == std::string::npos) break;
        out.append(json, position, begin - position);
        out += dir;
        position = end + 3;
        ++count;
    }
    if (count == 0) return 0;
    out.append(json, position, std::string::npos);
    json.swap(out);
    return count;
}

int json_int_after(const std::string &json, const char *anchor, const char *field) {
    const size_t anchor_at = json.find(anchor);
    if (anchor_at == std::string::npos) return 0;
    size_t at = json.find(std::string("\"") + field + "\"", anchor_at);
    if (at == std::string::npos) return 0;
    at = json.find(':', at);
    if (at == std::string::npos) return 0;
    return std::atoi(json.c_str() + at + 1);
}

long long file_bytes(const wchar_t *path) {
    WIN32_FILE_ATTRIBUTE_DATA data{};
    if (!::GetFileAttributesExW(path, GetFileExInfoStandard, &data)) return -1;
    return (static_cast<long long>(data.nFileSizeHigh) << 32) | data.nFileSizeLow;
}

bool mp4_finalized(const wchar_t *path) {
    const long long end = file_bytes(path);
    if (end < 64) return false;
    std::ifstream stream(fs::path(path), std::ios::binary);
    if (!stream) return false;
    bool done = false;
    for (long long position = 0, hop = 0; hop < 6 && position + 8 <= end; ++hop) {
        unsigned char header[8]{};
        stream.seekg(position, std::ios::beg);
        if (!stream.read(reinterpret_cast<char *>(header), 8)) break;
        std::uint64_t length = (static_cast<std::uint64_t>(header[0]) << 24) |
                               (static_cast<std::uint64_t>(header[1]) << 16) |
                               (static_cast<std::uint64_t>(header[2]) << 8) | header[3];
        if (length == 0) length = static_cast<std::uint64_t>(end) - static_cast<std::uint64_t>(position);
        if (header[4] == 'm' && header[5] == 'o' && header[6] == 'o' && header[7] == 'v') {
            done = static_cast<long long>(length) == end - position;
            break;
        }
        if (length < 8) break;
        position += static_cast<long long>(length);
    }
    return done;
}

std::string drop_extension(const fs::path &path) {
    return path_text(path.stem());
}

int parse_codec(const std::string &text) {
    if (text == "h264" || text == "avc") return 0;
    if (text == "hevc" || text == "h265") return 1;
    if (text == "av1") return 2;
    return std::atoi(text.c_str());
}

long long parse_bitrate(const std::string &text) {
    if (text.empty()) return 12000000LL;
    const char unit = text.back();
    const std::string digits = (unit == 'M' || unit == 'm' || unit == 'K' || unit == 'k')
                                   ? text.substr(0, text.size() - 1)
                                   : text;
    double value = std::atof(digits.c_str());
    if (unit == 'M' || unit == 'm') value *= 1000000.0;
    else if (unit == 'K' || unit == 'k') value *= 1000.0;
    return static_cast<long long>(value);
}

}

int run_export_video(const Options &options) {
    if (options.inputs.empty()) fail(EX_USAGE, "-ex needs a draft directory or a plain draft_content.json");

    const std::vector<InputItem> items = expand_inputs(options.inputs);
    if (items.size() > 1) return run_export_batch(options, items);
    trace_line("single export job");

    const fs::path input = items.front().path;
    std::error_code error;
    fs::path json_path;
    fs::path draft_dir;
    const bool dir_input = fs::is_directory(input, error);
    if (dir_input) {
        draft_dir = input;
        const fs::path plain = draft_dir / L"draft_plain.json";
        json_path = fs::is_regular_file(plain, error) ? plain : draft_dir / L"draft_content.json";
    } else {
        json_path = input;
        draft_dir = input.parent_path();
    }

    const std::string raw = read_text(json_path);
    if (raw.empty()) fail(EX_PARSE, "cannot read draft json, or it is empty: " + path_text(json_path));
    const bool plain = looks_like_plain_json(raw);
    note_line(label_text("draft") + path_text(json_path) + "  (" + num(static_cast<long long>(raw.size())) +
              " bytes" + (plain ? "" : ", encrypted") + ")");

    const auto canvas_of = [](const std::string &text) {
        return std::pair<int, int>{json_int_after(text, "\"canvas_config\"", "width"),
                                   json_int_after(text, "\"canvas_config\"", "height")};
    };
    fs::path out;
    if (options.out) {
        out = *options.out;
    } else if (dir_input) {
        out = draft_dir.parent_path() / (draft_dir.filename().wstring() + L".mp4");
    } else {
        out = draft_dir / (drop_extension(json_path) + ".mp4");
    }
    if (!out.is_absolute()) out = absolute_from(fs::current_path(), out);

    const double fps = static_cast<double>(options.fps.value_or(30));
    const long long bitrate = parse_bitrate(options.bitrate.value_or(std::string()));
    const int codec = parse_codec(options.codec);

    if (options.dry_run) {
        int estimate_width = options.width.value_or(0);
        int estimate_height = options.height.value_or(0);
        if (plain && (estimate_width <= 0 || estimate_height <= 0)) {
            const auto [declared_width, declared_height] = canvas_of(raw);
            if (declared_width > 0 && declared_height > 0) {
                note_line(label_text("canvas") + num(declared_width) + "x" + num(declared_height) +
                          " (from draft)");
                if (estimate_width <= 0) estimate_width = declared_width;
                if (estimate_height <= 0) estimate_height = declared_height;
            }
        }
        if (estimate_width <= 0 || estimate_height <= 0) {
            estimate_width = 1080;
            estimate_height = 1920;
            note_line(label_text("canvas") +
                      "draft is encrypted, --dry-run skips engine load, assuming 1080x1920");
        }
        note_line(label_text("output") + path_text(out) + "  (" + num(estimate_width) + "x" +
                  num(estimate_height) + " @ " + num(static_cast<long long>(fps)) + " fps, " + num(bitrate) +
                  " bps)");
        note_line(label_text("note") + "--dry-run, engine not loaded");
        return EX_OK;
    }

    Module module(options.jy_dir);
    if (fs::exists(out) && !options.force) {
        fail(EX_EXPORT, "output already exists; refusing overwrite: " + path_text(out));
    }
    if (options.force) ::DeleteFileW(out.c_str());
    std::string json = raw;
    if (!plain) {
        json = crypto_decrypt_text(module, raw, path_text(json_path));
        note_line(label_text("decrypt") + num(static_cast<long long>(json.size())) + " bytes in memory");
    }
    const int patched = patch_placeholders(json, draft_dir);
    note_line(label_text("timeline") + num(patched) + " placeholders resolved");

    int width = options.width.value_or(0);
    int height = options.height.value_or(0);
    if (width <= 0 || height <= 0) {
        const auto [declared_width, declared_height] = canvas_of(json);
        if (declared_width > 0 && declared_height > 0) {
            note_line(label_text("canvas") + num(declared_width) + "x" + num(declared_height) +
                      " (from draft)");
        }
        if (width <= 0) width = declared_width;
        if (height <= 0) height = declared_height;
    }
    if (width <= 0 || height <= 0) {
        width = 1080;
        height = 1920;
    }
    note_line(label_text("output") + path_text(out) + "  (" + num(width) + "x" + num(height) + " @ " +
              num(static_cast<long long>(fps)) + " fps, " + num(bitrate) + " bps)");

    ExportEngine engine(module);
    stage("resolve exports");
    engine.load();
    note_line(label_text("engine") + path_text(module.dll_path()) + "  abi=" + std::string(kAbi.version));

    stage("getLogger");
    void *logger = engine.get_logger()();
    stage("setAlogFunction");
    engine.set_alog_function()(logger, reinterpret_cast<void *>(&engine_log), nullptr);
    stage("setLogLevel");
    Str32 level;
    const std::string level_text = env_text("JYDC_LOG_LEVEL", g_verbose ? "debug" : "info");
    level.set(level_text);
    engine.set_log_level()(logger, &level);
    // Match the installed official lyra-cli's caption initialization (5.0).
    // This initializes the engine; it does not change a draft's font sizes.
    stage("initialize subtitle canvas metrics");
    engine.set_subtitle_canvas_vertical()(5.0);
    trace_line("engine log level=" + level_text);

    stage("Server::instance");
    void *server = engine.only_instance()();
    engine.set_server(server);
    std::uint8_t injector[64]{};
    stage("Server::startup");
    engine.startup()(server, nullptr, injector);
    Str32 session_name;
    session_name.set(std::string("isolated-local-export"));
    stage("Server::openSession");
    const std::int32_t session = engine.open_session()(server, nullptr, &session_name);
    engine.set_session(session);
    note_line(label_text("session") + "sid=" + num(session));
    if (session < 0) fail(EX_EXPORT, "openSession returned a negative session: " + num(session));

    Str32 json_string;
    json_string.set(json);
    Pair persistent{};
    stage("deserialize_persistent_draft");
    engine.deserializer()(&persistent, &json_string);
    stage("ProjectService::init build request");
    Pair init = engine.request(false, "ProjectService", "init", false, 0xB0);
    {
        auto *object = static_cast<unsigned char *>(init.object);
        write_at<std::int32_t>(object + O_TID, -1);
        reinterpret_cast<Str32 *>(object + 0x58)->set(std::string("isolated-local-export"));
        write_at<std::int32_t>(object + 0x78, -1);
        write_at<std::uint32_t>(object + 0x7C, 1);
        reinterpret_cast<Str32 *>(object + 0x80)->set(std::string("isolated-local-export"));
        write_at<Pair>(object + 0xA0, persistent);
    }
    Pair init_reply{};
    stage("ProjectService::init call");
    engine.project_client_init()(&init_reply, &init, session);
    const std::int32_t task = reply_task(init_reply.object);
    note_line(label_text("engine") + "init code=" + num(reply_code(init_reply.object)) + " tid=" + num(task));
    engine.pump(12);

    Pair session_object{};
    stage("Server::getSession");
    engine.get_session()(server, &session_object, session);
    Pair wrapper{};
    stage("getVeWrapper");
    engine.ve_wrapper()(session_object.object, &wrapper);
    if (!wrapper.object) fail(EX_EXPORT, "getVeWrapper returned no timeline editor");
    if (options.effect_mask_runtime) initialize_effect_masks(module, wrapper.object, options.jy_dir);
    stage("openTimelineEditor");
    engine.open_timeline_editor()(wrapper.object, task, 1);
    engine.pump(80);

    Pair draft{};
    stage("GetDraftFromJson");
    engine.draft_from_json()(&draft, &json_string);
    note_line(label_text("draft") + "object 0x" +
              num(static_cast<long long>(reinterpret_cast<std::uintptr_t>(draft.object))));
    stage("DraftService::draftInit build request");
    Pair draft_init = engine.request(true, "DraftService", "draftInit", true);
    {
        auto *object = static_cast<unsigned char *>(draft_init.object);
        write_at<std::int32_t>(object + O_TID, task);
        write_at<std::int32_t>(object + O_INKIND, 2);
        write_at<Pair>(object + O_PAYLOAD, draft);
        write_at<std::uint16_t>(object + O_BOOLS, 0x0100);
    }
    Pair draft_reply{};
    stage("DraftService::draftInit call");
    engine.invoke()(server, &draft_reply, &draft_init, session);
    note_line(label_text("draft") + "draftInit code=" + num(reply_code(draft_reply.object)));
    engine.pump(20);

    stage("restoreDraft call");
    engine.restore_draft()(session, 1, task);
    engine.pump(60);
    note_line(label_text("restore") + "waiting for draft restore");
    stage("restoreDraft wait");
    const unsigned long long wait_start = ::GetTickCount64();
    int waited = 0;
    for (; waited < 12000 && !g_signals.restored.load(); ++waited) {
        engine.pump_once()(server);
        ::Sleep(5);
    }
    const unsigned long long wait_ms = ::GetTickCount64() - wait_start;
    const std::string wait_text =
        num(static_cast<long long>(wait_ms / 1000)) + "." + num(static_cast<long long>(wait_ms % 1000 / 100)) + " s";
    trace_line("restore wait pumps=" + num(waited) + " restored=" + yes(g_signals.restored.load()) + " engine log calls=" +
               num(g_signals.log_calls.load()) + " elapsed=" + wait_text);
    note_line(label_text("restore") + (g_signals.restored.load() ? "restored" : "no restore marker, continuing") +
              " after " + wait_text);

    stage("Server::getSession timeline");
    engine.get_session()(server, &session_object, session);
    Pair timeline{};
    std::uint8_t callback_slot[64]{};
    stage("draftTransaction");
    engine.draft_transaction()(session_object.object, &timeline, callback_slot, task);
    note_line(label_text("draft") + "transaction 0x" +
              num(static_cast<long long>(reinterpret_cast<std::uintptr_t>(timeline.object))));
    engine.pump(40);

    Pair export_request{};
    stage("exportStart factory");
    engine.export_factory()(&export_request);
    if (!export_request.object) fail(EX_EXPORT, "engine did not build an export request");
    auto *object = static_cast<unsigned char *>(export_request.object);
    write_at<std::int32_t>(object + O_TID, task);
    reinterpret_cast<Str32 *>(object + O_OUT)->set(path_text(out), engine.operator_new());
    auto *config = object + O_CFG;
    write_at<std::int32_t>(config + O_CW, width);
    write_at<std::int32_t>(config + O_CH, height);
    write_at<std::uint8_t>(config + O_CHARD, static_cast<std::uint8_t>(options.hard_encode));
    write_at<double>(config + O_CFPS, fps);
    write_at<std::int32_t>(config + O_CCODEC, codec);
    write_at<std::int32_t>(config + O_CQUAL, options.quality);
    write_at<std::int64_t>(config + O_CBR, static_cast<std::int64_t>(bitrate));
    write_at<std::int32_t>(config + O_CCOLOR, options.color_space);
    write_at<std::uint8_t>(config + O_CMP4, static_cast<std::uint8_t>(options.mp4_flag));
    write_at<std::int32_t>(object + O_TAIL, 0);
    note_line(label_text("export") + "start service=" +
              std::string(reinterpret_cast<Str32 *>(object + O_SVC)->c_str()) + " api=" +
              std::string(reinterpret_cast<Str32 *>(object + O_API)->c_str()) + " tid=" + num(task));

    stage("export: invokeSync");
    engine.invoke_sync()(server, &export_request, callback_slot, session);

    const std::wstring out_wide = out.wstring();
    long long last = -1;
    long long written = 0;
    int stable = 0;
    const DWORD started = ::GetTickCount();
    DWORD printed = 0;
    const DWORD budget = static_cast<DWORD>(options.timeout_seconds) * 1000u;
    trace_line("export budget=" + num(options.timeout_seconds) + " s");
    for (;;) {
        engine.pump_once()(server);
        written = file_bytes(out_wide.c_str());
        if (written != last) {
            last = written;
            stable = 0;
            const DWORD now = ::GetTickCount();
            if (now - printed >= 1000) {
                printed = now;
                note_line("    " + num(static_cast<long long>((now - started) / 1000)) + "." +
                          num(static_cast<long long>(((now - started) % 1000) / 100)) + "s  output " +
                          num(written < 0 ? 0 : written) + " bytes");
            }
        } else {
            ++stable;
        }
        if (g_signals.finished.load() && stable > 200) break;
        if (stable > 100 && mp4_finalized(out_wide.c_str())) break;
        if (g_signals.failed.load()) break;
        if (::GetTickCount() - started > budget) {
            note_line(label_text("error") + "render not finished after " + num(options.timeout_seconds) +
                      " s, use --wait");
            break;
        }
        ::Sleep(10);
    }
    engine.pump(50);

    const long long final_size = file_bytes(out_wide.c_str());
    trace_line("export size=" + num(final_size) + " stable=" + num(stable) +
               " finished=" + yes(g_signals.finished.load()) + " failed=" + yes(g_signals.failed.load()));
    if (g_signals.failed.load() || final_size <= 0 || !mp4_finalized(out.c_str())) {
        std::string detail;
        {
            std::lock_guard<std::mutex> lock(g_signals.mutex);
            detail = g_signals.last_error;
        }
        note_line(label_text("error") + "export failed: " + path_text(out) + "  (" +
                  num(final_size < 0 ? 0 : final_size) + " bytes)");
        if (!detail.empty()) note_line("  engine: " + detail);
        return EX_EXPORT;
    }
    if (options.effect_mask_runtime) verified_effect_mask_resources(options.jy_dir);
    const DWORD spent = ::GetTickCount() - started;
    note_line(label_text("export") + path_text(out) + "  (" + num(final_size) + " bytes, " +
              num(static_cast<long long>(spent / 1000)) + "." +
              num(static_cast<long long>((spent % 1000) / 100)) + " s)");
    return EX_OK;
}

namespace {

bool references_string(const Module &module, std::uint32_t rva, size_t span, const char *needle) {
    const auto range = module.image_range();
    const unsigned char *start = static_cast<const unsigned char *>(module.at(rva));
    for (size_t i = 0; i + 7 <= span; ++i) {
        const unsigned char *at = start + i;
        if (at + 7 > range.second) break;
        if (at[0] != 0x48 || at[1] != 0x8D || (at[2] & 0xC7) != 0x05) continue;
        const char *target = reinterpret_cast<const char *>(at + 7 + read_at<std::int32_t>(at + 3));
        if (target < reinterpret_cast<const char *>(range.first) ||
            target >= reinterpret_cast<const char *>(range.second)) {
            continue;
        }
        if (std::strcmp(target, needle) == 0) return true;
    }
    return false;
}

}

bool self_check_export(Module &module) {
    bool healthy = true;
    note_line("  export profile: abi=" + std::string(kAbi.version));
    const auto range = module.image_range();
    const auto inside = [&range](std::uintptr_t address) {
        return address >= reinterpret_cast<std::uintptr_t>(range.first) &&
               address < reinterpret_cast<std::uintptr_t>(range.second);
    };

    const std::pair<std::string_view, std::string_view> named[] = {
        {name::kServerInstance, "instance"},
        {name::kServerStartup, "startup"},
        {name::kOpenSession, "openSession"},
        {name::kGetSession, "getSession"},
        {name::kInvoke, "invoke"},
        {name::kInvokeSync, "invokeSync"},
        {name::kPumpOnce, "pumpOnce"},
        {name::kGetVeWrapper, "getVeWrapper"},
        {name::kOpenTimelineEditor, "openTimelineEditor"},
        {name::kDraftTransaction, "draftTransaction"},
        {name::kProjectClientInit, "ProjectClient::init"},
        {name::kDeserializePersistentDraft, "deserialize_persistent_draft"},
        {name::kGetDraftFromJson, "GetDraftFromJson"},
        {name::kGetLogger, "getLogger"},
        {name::kSetAlogFunction, "setAlogFunction"},
        {name::kSetLogLevel, "setLogLevel"},
    };
    int resolved = 0;
    const long long named_total = static_cast<long long>(sizeof(named) / sizeof(named[0]));
    std::string missing;
    for (const auto &[symbol, label] : named) {
        FARPROC raw = ::GetProcAddress(module.handle(), std::string(symbol).c_str());
        if (raw && module.owns_code(reinterpret_cast<const void *>(raw))) {
            ++resolved;
        } else {
            missing += std::string(missing.empty() ? "" : ", ") + std::string(label);
        }
    }
    note_line("  exports   : " + num(resolved) + "/" + num(named_total) + " entries resolvable" +
              (missing.empty() ? std::string() : ", missing: " + missing));
    if (!missing.empty()) healthy = false;

    for (int i = 0; i < 2; ++i) {
        const std::uint32_t rva = i ? kAbi.vt_request : kAbi.vt_control;
        const auto *table = static_cast<const std::uint64_t *>(module.at(rva));
        const bool good = inside(table[-1]) && inside(table[0]) && inside(table[1]) && inside(table[2]);
        char detail[192]{};
        std::snprintf(detail, sizeof(detail), "rva 0x%08X [0]=%llX [1]=%llX [2]=%llX rtti=%llX", rva,
                      static_cast<unsigned long long>(table[0]), static_cast<unsigned long long>(table[1]),
                      static_cast<unsigned long long>(table[2]),
                      static_cast<unsigned long long>(table[-1]));
        note_line(std::string("  ") + (i ? "vt_request " : "vt_control ") + ok(good) + "  " + detail);
        if (!good) healthy = false;
    }
    const bool has_restore = references_string(module, kAbi.restore_draft, 0x600, "restoreDraft");
    note_line("  restoreDraft  " + ok(has_restore) + "  rva 0x" +
              module.rva_hex(module.at(kAbi.restore_draft)) + "  references \"restoreDraft\"");
    if (!has_restore) healthy = false;
    const bool has_factory = references_string(module, kAbi.export_factory, 0x900, "exportStart");
    note_line("  exportFactory " + ok(has_factory) + "  rva 0x" +
              module.rva_hex(module.at(kAbi.export_factory)) + "  references \"exportStart\"");
    if (!has_factory) healthy = false;

    using FnNew = void *(*)(size_t);
    auto allocate = reinterpret_cast<FnNew>(module.at(kAbi.operator_new));
    void *block = module.owns_code(reinterpret_cast<const void *>(allocate)) ? allocate(64) : nullptr;
    const bool allocates = block && (std::memset(block, 0xAB, 64), static_cast<unsigned char *>(block)[63] == 0xAB);
    note_line("  operator new  " + ok(allocates) + "  rva 0x" + module.rva_hex(module.at(kAbi.operator_new)) +
              "  returns " + num(static_cast<long long>(reinterpret_cast<std::uintptr_t>(block))));
    if (!allocates) healthy = false;

    note_line(healthy ? "  => export path matches this dll"
                      : "  => export path has mismatches, do not export with it");
    return healthy;
}

}
