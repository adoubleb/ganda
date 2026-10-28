// Every JaCoCo probe of every OrientDB class mapped to the source lines it proves
// executed, so that a probe set (one coverage window of `collector.py`) becomes a line
// set offline, with JaCoCo's own line semantics. Run through code_coverage/probe_lines.py,
// which builds the classpath (org.jacoco.core / org.jacoco.report / ASM from ~/.m2) and
// extracts the jars from the image; launched as a single-file source program, as
// CoverageReport.java is.
//
//   map    --lib DIR [--exclude C...] --out FILE [--threads N]
//   verify --lib DIR [--exclude C...] --map FILE [--samples N] [--largest N] [--draws N] [--seed S] [--every-probe] [--threads N]
//   lines  --lib DIR [--exclude C...] --out FILE [--exec F...]
//
// `map` writes gzip-compressed JSON lines: a header, then one record per class (the
// jars in name order, the classes in entry order, as CoverageReport.java sees them):
// the class id as the agent computes it (CRC64 of the class bytes), the vm name, the
// package and source file, the probe count, the executable lines (every line with an
// instruction) and, for each probe i, the lines whose status is partly or fully
// covered when probe i ALONE is hit. JaCoCo analyzes the methods of a class one by one
// (a method's instructions read only the probes of that method), so one analysis
// answers one probe of EVERY method at once: batch k hits the k-th probe of each
// method and reads each method's own lines, and a class costs as many analyses as its
// largest method has probes, not as many as the class has. `verify` checks that
// shortcut and the map's whole premise against direct analyses -- singletons, and
// random probe subsets whose lines must be the union of their probes' lines.
// `lines` is JaCoCo itself on execution data (none: the executable lines alone): the
// covered and executable lines by source file and the line counters by package, which
// probe_lines.py compares with the map.

import java.io.*;
import java.nio.charset.StandardCharsets;
import java.nio.file.*;
import java.util.*;
import java.util.concurrent.*;
import java.util.zip.*;

import org.jacoco.core.JaCoCo;
import org.jacoco.core.analysis.*;
import org.jacoco.core.data.*;
import org.jacoco.core.internal.data.CRC64;
import org.jacoco.core.internal.flow.ClassProbesAdapter;
import org.jacoco.core.internal.flow.ClassProbesVisitor;
import org.jacoco.core.internal.flow.MethodProbesVisitor;
import org.jacoco.core.internal.instr.InstrSupport;
import org.jacoco.core.tools.ExecFileLoader;
import org.objectweb.asm.ClassReader;
import org.objectweb.asm.ClassVisitor;
import org.objectweb.asm.MethodVisitor;

public class ProbeLines {

    static final String FORMAT = "ganda-probe-lines/1";

    record ClassFile(String jar, String name, byte[] bytes) {}

    /** The probes of a class by method: `methods.get(m)` owns probes [first[m], first[m] + count[m]). */
    record Layout(int probes, List<String> methods, int[] first, int[] count) {}

    /** A class's lines: the executable ones and, per probe, the ones it alone covers. */
    record Lines(IClassCoverage empty, int[] executable, int[][] probeLines) {}

    public static void main(String[] argv) throws Exception {
        if (argv.length == 0) usage();
        String cmd = argv[0];
        Map<String, List<String>> opt = parse(Arrays.copyOfRange(argv, 1, argv.length));
        List<ClassFile> classes = loadClasses(one(opt, "lib"), opt.getOrDefault("exclude", List.of()));
        int threads = opt.containsKey("threads") ? Integer.parseInt(one(opt, "threads")) : Runtime.getRuntime().availableProcessors();
        switch (cmd) {
            case "map" -> map(opt, classes, threads);
            case "verify" -> System.exit(verify(opt, classes, threads));
            case "lines" -> lines(opt, classes);
            default -> usage();
        }
    }

    static void usage() {
        System.err.println("usage: ProbeLines map --lib DIR [--exclude C...] --out FILE [--threads N]\n"
                + "       ProbeLines verify --lib DIR [--exclude C...] --map FILE [--samples N] [--largest N] [--draws N] [--seed S] [--every-probe] [--threads N]\n"
                + "       ProbeLines lines --lib DIR [--exclude C...] --out FILE [--exec F...]");
        System.exit(2);
    }

    static Map<String, List<String>> parse(String[] args) {
        Map<String, List<String>> m = new HashMap<>();
        String key = null;
        for (String a : args) {
            if (a.startsWith("--")) {
                key = a.substring(2);
                m.computeIfAbsent(key, k -> new ArrayList<>());
            } else if (key != null) {
                m.get(key).add(a);
            }
        }
        return m;
    }

    static String one(Map<String, List<String>> opt, String key) {
        List<String> v = opt.get(key);
        if (v == null || v.isEmpty()) {
            System.err.println("missing --" + key);
            usage();
        }
        return v.get(0);
    }

    /** CoverageReport.loadClasses: every com/orientechnologies class of the orientdb-*.jars
     * less the agent's excludes; a name a second jar repeats is the first jar's. */
    static List<ClassFile> loadClasses(String lib, List<String> exclude) throws IOException {
        Set<String> skip = new HashSet<>();
        for (String x : exclude) skip.add(x.replace('.', '/'));
        List<Path> jars = new ArrayList<>();
        try (DirectoryStream<Path> ds = Files.newDirectoryStream(Paths.get(lib), "orientdb-*.jar")) {
            ds.forEach(jars::add);
        }
        Collections.sort(jars);
        List<ClassFile> out = new ArrayList<>();
        Set<String> seen = new HashSet<>();
        for (Path jar : jars) {
            String bundle = jar.getFileName().toString().replaceAll("-\\d[\\d.]*\\.jar$", "");
            try (ZipFile z = new ZipFile(jar.toFile())) {
                for (ZipEntry e : Collections.list(z.entries())) {
                    String n = e.getName();
                    if (!n.startsWith("com/orientechnologies/") || !n.endsWith(".class")) continue;
                    if (skip.contains(n.substring(0, n.length() - ".class".length()))) continue;
                    if (!seen.add(n)) {
                        System.err.println("skipping duplicate " + n + " in " + jar.getFileName());
                        continue;
                    }
                    try (InputStream in = z.getInputStream(e)) {
                        out.add(new ClassFile(bundle, n, in.readAllBytes()));
                    }
                }
            }
        }
        if (out.isEmpty()) throw new IOException("no com/orientechnologies classes in " + lib + "/orientdb-*.jar");
        return out;
    }

    // --- one class ------------------------------------------------------------------

    static String vmName(ClassFile c) {
        return c.name().substring(0, c.name().length() - ".class".length());
    }

    /** Which method owns which probe, numbered as the agent and the analyzer number them
     * (ClassProbesAdapter hands out the ids method by method, in class-file order). */
    static Layout layout(byte[] bytes) {
        List<String> methods = new ArrayList<>();
        List<Integer> owner = new ArrayList<>();
        int[] current = {-1};
        ClassProbesVisitor sink = new ClassProbesVisitor() {
            @Override
            public MethodProbesVisitor visitMethod(int access, String name, String desc, String signature, String[] exceptions) {
                return new MethodProbesVisitor() {};
            }

            @Override
            public void visitTotalProbeCount(int count) {}
        };
        ClassProbesAdapter adapter = new ClassProbesAdapter(sink, false) {
            @Override
            public int nextId() {
                owner.add(current[0]);
                return super.nextId();
            }
        };
        // the adapter buffers a method and numbers its probes at the method's end, which
        // the reader reaches before it visits the next method
        ClassVisitor front = new ClassVisitor(InstrSupport.ASM_API_VERSION, adapter) {
            @Override
            public MethodVisitor visitMethod(int access, String name, String desc, String signature, String[] exceptions) {
                current[0] = methods.size();
                methods.add(name + desc);
                return super.visitMethod(access, name, desc, signature, exceptions);
            }
        };
        InstrSupport.classReaderFor(bytes).accept(front, 0);
        int[] first = new int[methods.size()], count = new int[methods.size()];
        Arrays.fill(first, -1);
        for (int id = 0; id < owner.size(); id++) {
            int m = owner.get(id);
            if (first[m] < 0) first[m] = id;
            if (first[m] + count[m] != id) throw new IllegalStateException("probes of " + methods.get(m) + " are not contiguous");
            count[m]++;
        }
        return new Layout(owner.size(), methods, first, count);
    }

    /** JaCoCo's analysis of one class with exactly `hit` probes hit (null: no execution
     * data); null when the analyzer skips the class (a synthetic class). */
    static IClassCoverage analyze(ClassFile c, long id, boolean[] hit) throws IOException {
        ExecutionDataStore store = new ExecutionDataStore();
        if (hit != null) store.put(new ExecutionData(id, vmName(c), hit));
        IClassCoverage[] out = {null};
        new Analyzer(store, cc -> out[0] = cc).analyzeClass(c.bytes(), c.jar() + "!" + c.name());
        return out[0];
    }

    static boolean covered(int status) {
        return status == ICounter.FULLY_COVERED || status == ICounter.PARTLY_COVERED;
    }

    static int[] coveredLines(ISourceNode n) {
        List<Integer> out = new ArrayList<>();
        for (int l = n.getFirstLine(); l >= 0 && l <= n.getLastLine(); l++)
            if (covered(n.getLine(l).getStatus())) out.add(l);
        return out.stream().mapToInt(Integer::intValue).toArray();
    }

    static int[] executableLines(ISourceNode n) {
        List<Integer> out = new ArrayList<>();
        for (int l = n.getFirstLine(); l >= 0 && l <= n.getLastLine(); l++)
            if (n.getLine(l).getStatus() != ICounter.EMPTY) out.add(l);
        return out.stream().mapToInt(Integer::intValue).toArray();
    }

    static Lines probeLines(ClassFile c, long id, Layout layout) throws IOException {
        IClassCoverage empty = analyze(c, id, null);
        int[][] probeLines = new int[layout.probes()][];
        Arrays.fill(probeLines, new int[0]);
        if (empty == null) return new Lines(null, new int[0], probeLines);
        Map<String, Integer> index = new HashMap<>();
        for (int m = 0; m < layout.methods().size(); m++) index.put(layout.methods().get(m), m);
        int batches = Arrays.stream(layout.count()).max().orElse(0);
        for (int k = 0; k < batches; k++) {
            boolean[] hit = new boolean[layout.probes()];
            for (int m = 0; m < layout.count().length; m++)
                if (layout.count()[m] > k) hit[layout.first()[m] + k] = true;
            IClassCoverage cc = analyze(c, id, hit);
            for (IMethodCoverage mc : cc.getMethods()) {
                int m = index.get(mc.getName() + mc.getDesc());
                if (layout.count()[m] > k) probeLines[layout.first()[m] + k] = coveredLines(mc);
            }
        }
        return new Lines(empty, executableLines(empty), probeLines);
    }

    // --- map ------------------------------------------------------------------------

    static void map(Map<String, List<String>> opt, List<ClassFile> classes, int threads) throws Exception {
        long start = System.nanoTime();
        ExecutorService pool = Executors.newFixedThreadPool(threads);
        List<Future<ClassRecord>> records = new ArrayList<>();
        for (ClassFile c : classes) records.add(pool.submit(() -> record(c)));
        Set<Long> ids = new HashSet<>();
        int probes = 0, synthetic = 0, noSource = 0, noLines = 0;
        Map<String, String> sources = new HashMap<>();
        try (Writer out = new OutputStreamWriter(new GZIPOutputStream(new FileOutputStream(one(opt, "out")), 1 << 16), StandardCharsets.UTF_8)) {
            out.write(header(classes) + "\n");
            for (int i = 0; i < classes.size(); i++) {
                ClassRecord r = records.get(i).get();
                out.write(r.json());
                out.write('\n');
                if (!ids.add(r.id())) throw new IllegalStateException("two classes share the id " + r.id());
                probes += r.probes();
                if (!r.analyzed()) synthetic++;
                else if (r.path() == null) noSource++;
                else if (r.lines() == 0) noLines++;
                if (r.path() != null) {
                    String prev = sources.putIfAbsent(r.path(), classes.get(i).jar());
                    if (prev != null && !prev.equals(classes.get(i).jar()))
                        System.err.println("source file " + r.path() + " has classes in " + prev + " and " + classes.get(i).jar());
                }
            }
        } finally {
            pool.shutdown();
        }
        System.out.printf("%d classes, %d probes, %d source files -> %s in %.1f s%n", classes.size(), probes, sources.size(), one(opt, "out"), (System.nanoTime() - start) / 1e9);
        System.out.printf("  skipped by the analyzer (synthetic): %d; without a source file: %d; analyzed but without lines: %d%n", synthetic, noSource, noLines);
    }

    static String header(List<ClassFile> classes) {
        Set<String> jars = new TreeSet<>();
        for (ClassFile c : classes) jars.add(c.jar());
        StringBuilder sb = new StringBuilder("{\"format\":").append(Json.str(FORMAT))
                .append(",\"jacoco\":").append(Json.str(JaCoCo.VERSION))
                .append(",\"classes\":").append(classes.size()).append(",\"jars\":[");
        String sep = "";
        for (String j : jars) {
            sb.append(sep).append(Json.str(j));
            sep = ",";
        }
        return sb.append("]}").toString();
    }

    /** A class's JSON line, and what the summary counts of it. */
    record ClassRecord(String json, long id, int probes, boolean analyzed, String path, int lines) {}

    static ClassRecord record(ClassFile c) throws IOException {
        long id = CRC64.classId(c.bytes());
        Layout layout = layout(c.bytes());
        Lines lines = probeLines(c, id, layout);
        IClassCoverage cc = lines.empty();
        String name = vmName(c);
        String pkg = name.contains("/") ? name.substring(0, name.lastIndexOf('/')) : "";
        StringBuilder sb = new StringBuilder("{\"id\":").append(id)
                .append(",\"name\":").append(Json.str(name))
                .append(",\"jar\":").append(Json.str(c.jar()))
                .append(",\"package\":").append(Json.str(pkg))
                .append(",\"source\":").append(cc == null || cc.getSourceFileName() == null ? "null" : Json.str(cc.getSourceFileName()))
                .append(",\"analyzed\":").append(cc != null)
                .append(",\"probes\":").append(layout.probes())
                .append(",\"lines\":");
        Json.ints(sb, lines.executable());
        sb.append(",\"probe_lines\":[");
        for (int i = 0; i < lines.probeLines().length; i++) {
            if (i > 0) sb.append(',');
            Json.ints(sb, lines.probeLines()[i]);
        }
        String path = cc == null || cc.getSourceFileName() == null ? null : pkg + "/" + cc.getSourceFileName();
        return new ClassRecord(sb.append("]}").toString(), id, layout.probes(), cc != null, path, lines.executable().length);
    }

    // --- verify ---------------------------------------------------------------------

    /** The classes the map holds, by id: vm name, probe count, executable lines, probe lines. */
    record Mapped(String name, int probes, int[] executable, int[][] probeLines) {}

    static Map<Long, Mapped> readMap(String path) throws IOException {
        Map<Long, Mapped> out = new HashMap<>();
        try (BufferedReader in = new BufferedReader(new InputStreamReader(new GZIPInputStream(new FileInputStream(path), 1 << 16), StandardCharsets.UTF_8))) {
            Map<String, Object> header = Json.object(in.readLine());
            if (!FORMAT.equals(header.get("format"))) throw new IOException(path + " is not a " + FORMAT + " file");
            for (String line; (line = in.readLine()) != null; ) {
                Map<String, Object> r = Json.object(line);
                List<?> pl = (List<?>) r.get("probe_lines");
                int[][] probeLines = new int[pl.size()][];
                for (int i = 0; i < pl.size(); i++) probeLines[i] = Json.toInts((List<?>) pl.get(i));
                out.put((Long) r.get("id"), new Mapped((String) r.get("name"), ((Long) r.get("probes")).intValue(), Json.toInts((List<?>) r.get("lines")), probeLines));
            }
        }
        return out;
    }

    record Check(int subsets, int probes, List<String> mismatches) {}

    /** `samples` classes at random (0: every class with a probe; the `largest` ones always
     * among them); for each, random single probes (direct analysis against the map's
     * entry, which also checks the batching; `--every-probe`: each probe of the class),
     * `draws` random subsets of each of several sizes (direct analysis against the union
     * of their probes' entries), and the executable lines against the empty analysis. */
    static int verify(Map<String, List<String>> opt, List<ClassFile> classes, int threads) throws Exception {
        long start = System.nanoTime();
        Map<Long, Mapped> map = readMap(one(opt, "map"));
        int samples = opt.containsKey("samples") ? Integer.parseInt(one(opt, "samples")) : 300;
        long seed = opt.containsKey("seed") ? Long.parseLong(one(opt, "seed")) : 1;
        int largest = opt.containsKey("largest") ? Integer.parseInt(one(opt, "largest")) : 30;
        int draws = opt.containsKey("draws") ? Integer.parseInt(one(opt, "draws")) : 3;
        boolean everyProbe = opt.containsKey("every-probe");

        List<ClassFile> probed = new ArrayList<>();
        Map<ClassFile, Long> idOf = new HashMap<>();
        int missing = 0;
        for (ClassFile c : classes) {
            long id = CRC64.classId(c.bytes());
            Mapped m = map.get(id);
            if (m == null || !m.name().equals(vmName(c))) {
                missing++;
                continue;
            }
            idOf.put(c, id);
            if (m.probes() > 0) probed.add(c);
        }
        if (missing > 0 || map.size() != classes.size()) {
            System.out.printf("the map does not match the jars: %d of %d classes missing, %d records%n", missing, classes.size(), map.size());
            return 1;
        }
        // the largest classes, then a random sample of the rest
        List<ClassFile> bySize = new ArrayList<>(probed);
        bySize.sort(Comparator.comparingInt((ClassFile c) -> -map.get(idOf.get(c)).probes()).thenComparing(ClassFile::name));
        LinkedHashSet<ClassFile> chosen = new LinkedHashSet<>(bySize.subList(0, Math.min(largest, bySize.size())));
        List<ClassFile> rest = new ArrayList<>(probed);
        Collections.shuffle(rest, new Random(seed));
        for (ClassFile c : rest) {
            if (samples > 0 && chosen.size() >= samples) break;
            chosen.add(c);
        }

        ExecutorService pool = Executors.newFixedThreadPool(threads);
        List<ClassFile> order = new ArrayList<>(chosen);
        List<Future<Check>> checks = new ArrayList<>();
        for (int i = 0; i < order.size(); i++) {
            ClassFile c = order.get(i);
            long classSeed = seed * 1_000_003L + i;
            checks.add(pool.submit(() -> check(c, idOf.get(c), map.get(idOf.get(c)), new Random(classSeed), draws, everyProbe)));
        }
        int subsets = 0, probes = 0;
        List<String> mismatches = new ArrayList<>();
        try {
            for (Future<Check> f : checks) {
                Check r = f.get();
                subsets += r.subsets();
                probes += r.probes();
                mismatches.addAll(r.mismatches());
            }
        } finally {
            pool.shutdown();
        }
        long totalProbes = 0;
        for (ClassFile c : order) totalProbes += map.get(idOf.get(c)).probes();
        System.out.printf("verified %d classes (%d probes; the %d largest, up to %d probes, and %d at random, seed %d): %d analyses of %d probes in all, %.1f s%n",
                order.size(), totalProbes, Math.min(largest, order.size()), map.get(idOf.get(bySize.get(0))).probes(), order.size() - Math.min(largest, order.size()), seed,
                subsets, probes, (System.nanoTime() - start) / 1e9);
        if (mismatches.isEmpty()) {
            System.out.println("every subset's lines are the union of its probes' lines, every single probe's are the map's, and every class's executable lines are the map's");
            return 0;
        }
        System.out.printf("%d MISMATCHES%n", mismatches.size());
        mismatches.stream().limit(40).forEach(m -> System.out.println("  " + m));
        return 1;
    }

    static final int[] SIZES = {1, 2, 3, 5, 10, 30, 100, 1000};

    static Check check(ClassFile c, long id, Mapped m, Random rnd, int draws, boolean everyProbe) throws IOException {
        List<String> bad = new ArrayList<>();
        int subsets = 0, probes = 0;
        IClassCoverage empty = analyze(c, id, null);
        int[] executable = empty == null ? new int[0] : executableLines(empty);
        if (!Arrays.equals(executable, m.executable())) bad.add(vmName(c) + ": executable lines differ");
        int n = m.probes();
        // subset sizes: the fixed ones below n, half, all but one, all
        List<Integer> sizes = new ArrayList<>();
        for (int s : SIZES) if (s < n) sizes.add(s);
        for (int s : new int[]{n / 2, n - 1, n}) if (s > 0 && !sizes.contains(s)) sizes.add(s);
        for (int s : sizes) {
            int k = s == 1 ? (everyProbe ? n : Math.min(n, 20)) : s == n ? 1 : draws;
            for (int d = 0; d < k; d++) {
                int[] subset = s == 1 ? new int[]{everyProbe ? d : rnd.nextInt(n)} : sample(n, s, rnd);
                boolean[] hit = new boolean[n];
                TreeSet<Integer> union = new TreeSet<>();
                for (int p : subset) {
                    hit[p] = true;
                    for (int l : m.probeLines()[p]) union.add(l);
                }
                IClassCoverage cc = analyze(c, id, hit);
                int[] direct = cc == null ? new int[0] : coveredLines(cc);
                int[] mapped = union.stream().mapToInt(Integer::intValue).toArray();
                subsets++;
                probes += s;
                if (!Arrays.equals(direct, mapped)) {
                    Set<Integer> a = new TreeSet<>(), b = new TreeSet<>(union);
                    for (int l : direct) a.add(l);
                    Set<Integer> onlyDirect = new TreeSet<>(a), onlyMap = new TreeSet<>(b);
                    onlyDirect.removeAll(b);
                    onlyMap.removeAll(a);
                    bad.add(String.format("%s: %d probes %s: JaCoCo-only lines %s, map-only lines %s", vmName(c), s,
                            s <= 5 ? Arrays.toString(subset) : "", onlyDirect, onlyMap));
                }
            }
        }
        return new Check(subsets, probes, bad);
    }

    /** `k` distinct probes of `n`, in order (Floyd's sample). */
    static int[] sample(int n, int k, Random rnd) {
        TreeSet<Integer> s = new TreeSet<>();
        for (int j = n - k; j < n; j++) {
            int t = rnd.nextInt(j + 1);
            if (!s.add(t)) s.add(j);
        }
        return s.stream().mapToInt(Integer::intValue).toArray();
    }

    // --- lines ----------------------------------------------------------------------

    /** JaCoCo's own answer for execution data: one bundle per jar, as CoverageReport.java
     * builds them; lines by source file (a class without one under its vm name) and the
     * line counters by package. */
    static void lines(Map<String, List<String>> opt, List<ClassFile> classes) throws IOException {
        ExecFileLoader loader = new ExecFileLoader();
        for (String f : opt.getOrDefault("exec", List.of())) loader.load(new File(f));
        ExecutionDataStore store = loader.getExecutionDataStore();
        Map<String, CoverageBuilder> builders = new TreeMap<>();
        for (ClassFile c : classes) {
            CoverageBuilder b = builders.computeIfAbsent(c.jar(), k -> new CoverageBuilder());
            new Analyzer(store, b).analyzeClass(c.bytes(), c.jar() + "!" + c.name());
        }
        Map<String, int[]> covered = new TreeMap<>(), executable = new TreeMap<>();
        Map<String, long[]> packages = new TreeMap<>();
        int noMatch = 0;
        for (Map.Entry<String, CoverageBuilder> e : builders.entrySet()) {
            IBundleCoverage bundle = e.getValue().getBundle(e.getKey());
            noMatch += e.getValue().getNoMatchClasses().size();
            for (IPackageCoverage p : bundle.getPackages()) {
                long[] row = packages.computeIfAbsent(p.getName(), k -> new long[2]);
                row[0] += p.getLineCounter().getCoveredCount();
                row[1] += p.getLineCounter().getTotalCount();
                for (ISourceFileCoverage s : p.getSourceFiles()) {
                    String path = p.getName().isEmpty() ? s.getName() : p.getName() + "/" + s.getName();
                    put(covered, path, coveredLines(s));
                    put(executable, path, executableLines(s));
                }
                for (IClassCoverage cc : p.getClasses()) {
                    if (cc.getSourceFileName() != null) continue;
                    put(covered, cc.getName(), coveredLines(cc));
                    put(executable, cc.getName(), executableLines(cc));
                }
            }
        }
        try (Writer out = new OutputStreamWriter(new GZIPOutputStream(new FileOutputStream(one(opt, "out"))), StandardCharsets.UTF_8)) {
            StringBuilder sb = new StringBuilder("{\"no_match\":").append(noMatch).append(",\"packages\":{");
            String sep = "";
            for (Map.Entry<String, long[]> e : packages.entrySet()) {
                sb.append(sep).append(Json.str(e.getKey())).append(":[").append(e.getValue()[0]).append(',').append(e.getValue()[1]).append(']');
                sep = ",";
            }
            sb.append("},\"covered\":");
            linesObject(sb, covered);
            sb.append(",\"executable\":");
            linesObject(sb, executable);
            out.write(sb.append("}\n").toString());
        }
        if (noMatch > 0) System.err.println(noMatch + " classes whose execution data does not match the jars (a different build?)");
    }

    /** Two jars may hold classes of one source file; JaCoCo reports each jar's apart. */
    static void put(Map<String, int[]> m, String path, int[] lines) {
        if (lines.length == 0) return;
        int[] prev = m.get(path);
        if (prev == null) {
            m.put(path, lines);
            return;
        }
        TreeSet<Integer> s = new TreeSet<>();
        for (int l : prev) s.add(l);
        for (int l : lines) s.add(l);
        m.put(path, s.stream().mapToInt(Integer::intValue).toArray());
    }

    static void linesObject(StringBuilder sb, Map<String, int[]> m) {
        sb.append('{');
        String sep = "";
        for (Map.Entry<String, int[]> e : m.entrySet()) {
            sb.append(sep).append(Json.str(e.getKey())).append(':');
            Json.ints(sb, e.getValue());
            sep = ",";
        }
        sb.append('}');
    }

    // --- JSON -----------------------------------------------------------------------

    /** What the map needs of JSON: objects, arrays, strings, integers, booleans, null. */
    static final class Json {
        final String s;
        int i;

        Json(String s) {
            this.s = s;
        }

        @SuppressWarnings("unchecked")
        static Map<String, Object> object(String line) {
            return (Map<String, Object>) new Json(line).value();
        }

        static int[] toInts(List<?> xs) {
            int[] out = new int[xs.size()];
            for (int k = 0; k < out.length; k++) out[k] = ((Long) xs.get(k)).intValue();
            return out;
        }

        static void ints(StringBuilder sb, int[] xs) {
            sb.append('[');
            for (int k = 0; k < xs.length; k++) {
                if (k > 0) sb.append(',');
                sb.append(xs[k]);
            }
            sb.append(']');
        }

        static String str(String v) {
            StringBuilder sb = new StringBuilder("\"");
            for (char ch : v.toCharArray()) {
                if (ch == '"' || ch == '\\') sb.append('\\').append(ch);
                else if (ch < 0x20) sb.append(String.format("\\u%04x", (int) ch));
                else sb.append(ch);
            }
            return sb.append('"').toString();
        }

        void ws() {
            while (i < s.length() && Character.isWhitespace(s.charAt(i))) i++;
        }

        /** The next character past any whitespace, consumed. */
        char next() {
            ws();
            return s.charAt(i++);
        }

        Object value() {
            ws();
            char ch = s.charAt(i);
            switch (ch) {
                case '{' -> {
                    Map<String, Object> m = new LinkedHashMap<>();
                    i++;
                    ws();
                    if (s.charAt(i) == '}') {
                        i++;
                        return m;
                    }
                    while (true) {
                        String k = (String) value();
                        expect(':');
                        m.put(k, value());
                        if (next() == '}') return m;
                    }
                }
                case '[' -> {
                    List<Object> l = new ArrayList<>();
                    i++;
                    ws();
                    if (s.charAt(i) == ']') {
                        i++;
                        return l;
                    }
                    while (true) {
                        l.add(value());
                        if (next() == ']') return l;
                    }
                }
                case '"' -> {
                    StringBuilder sb = new StringBuilder();
                    i++;
                    while (true) {
                        char c = s.charAt(i++);
                        if (c == '"') return sb.toString();
                        if (c != '\\') {
                            sb.append(c);
                            continue;
                        }
                        char esc = s.charAt(i++);
                        switch (esc) {
                            case 'u' -> {
                                sb.append((char) Integer.parseInt(s.substring(i, i + 4), 16));
                                i += 4;
                            }
                            case 'n' -> sb.append('\n');
                            case 't' -> sb.append('\t');
                            default -> sb.append(esc);
                        }
                    }
                }
                case 'n' -> {
                    i += 4;
                    return null;
                }
                case 't' -> {
                    i += 4;
                    return true;
                }
                case 'f' -> {
                    i += 5;
                    return false;
                }
                default -> {
                    int j = i;
                    while (i < s.length() && (s.charAt(i) == '-' || Character.isDigit(s.charAt(i)))) i++;
                    return Long.parseLong(s.substring(j, i));
                }
            }
        }

        void expect(char c) {
            if (next() != c) throw new IllegalArgumentException("expected '" + c + "' at " + (i - 1));
        }
    }
}
