/*
 * A minimal pipeline that consumes a PLINK 1 trio named in a manifest, the way a real pipeline does:
 * the manifest is read when the workflow runs, and the trio is staged into a task that reads it.
 *
 * Without the plugin it runs on the bundled example files. With the plugin enabled, point
 * `--manifest` at the file the `biofuse` scope writes; the local FUSE tier drives it that way.
 */

params.manifest = "${projectDir}/example/manifest.csv"
// Test hooks for the local FUSE tier's lifecycle matrix: hold the task, fail it, stream the .bed slowly,
// and touch a marker file once the trio has been read.
params.sleep = 0
params.fail = false
params.stream = false
params.marker = null

process READ_TRIO {
    input:
    tuple val(id), path(bed), path(bim), path(fam)

    output:
    stdout

    script:
    """
    printf '%s bed magic: ' ${id}
    head -c 3 ${bed} | od -An -tx1 | tr -d ' \\n'
    printf ', variants: %s, samples: %s\\n' "\$(wc -l < ${bim})" "\$(wc -l < ${fam})"
    ${params.marker ? "touch '${params.marker}'" : ''}
    ${params.stream ? "timeout 60 cat ${bed} | (sleep 30; cat > /dev/null)" : ''}
    sleep ${params.sleep}
    ${params.fail ? 'exit 42' : ''}
    """
}

// Relative paths in the manifest are relative to the manifest itself.
def member(manifest, String path) {
    return path.startsWith('/') ? file(path) : file(manifest).parent.resolve(path)
}

workflow {
    channel.fromPath(params.manifest)
        .splitCsv(header: true)
        .map { row -> tuple(row.id, member(params.manifest, row.bed), member(params.manifest, row.bim), member(params.manifest, row.fam)) }
        | READ_TRIO
        | view
}
