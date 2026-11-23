// Serialize a user-owned draft through the exact 11.6 model parser.
// No version rewriting, migrations, editor startup, or internal RVAs.
// jy-draftc header: MIT, see research/jy-draftc/LICENSE.
#include "jy-draftc/src/11.6.0.14552/jy_draftc.h"

struct Pair { void *object; void *control; };
using FromJson = void (*)(Pair *, const std::string *);
using ToJson = void (*)(std::string *, const Pair *);

int wmain(int argc, wchar_t **argv) {
    using namespace jydc;
    ::SetErrorMode(SEM_FAILCRITICALERRORS | SEM_NOGPFAULTERRORBOX | SEM_NOOPENFILEERRORBOX);
    int result = EX_OTHER;
    try {
        if (argc != 4) fail(EX_USAGE, "runtime_directory input_json output_json");
        const fs::path runtime = fs::absolute(argv[1]);
        const fs::path input = fs::absolute(argv[2]);
        const fs::path output = fs::absolute(argv[3]);
        if (fs::exists(output)) fail(EX_OTHER, "output already exists");
        const std::string raw = read_text(input);
        if (!looks_like_plain_json(raw)) fail(EX_PARSE, "input must be a plaintext draft");
        auto *module = new Module(runtime);
        auto parse = module->require<FromJson>(name::kGetDraftFromJson, "GetDraftFromJson");
        auto serialize = module->require<ToJson>(name::kGetJsonFromDraft, "GetJsonFromDraft");
        Pair draft{};
        parse(&draft, &raw);
        if (!draft.object) fail(EX_PARSE, "native model parser returned no draft");
        std::string normalized;
        serialize(&normalized, &draft);
        if (!looks_like_plain_json(normalized)) fail(EX_PARSE, "native serializer returned no JSON");
        write_text_atomic(output, normalized);
        std::printf("model roundtrip bytes=%zu output_bytes=%zu\n", raw.size(), normalized.size());
        result = EX_OK;
    } catch (const Failure &failure) {
        std::fprintf(stderr, "error (%d): %s\n", failure.code, failure.what());
        result = failure.code;
    } catch (const std::exception &error) {
        std::fprintf(stderr, "error: %s\n", error.what());
    }
    std::fflush(nullptr);
    ::ExitProcess(result);
}
