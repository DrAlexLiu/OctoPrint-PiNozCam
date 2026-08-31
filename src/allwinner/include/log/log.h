#ifndef PINOZCAM_VENDOR_LOG_H_
#define PINOZCAM_VENDOR_LOG_H_

#include <stdio.h>

/* stdout is the daemon protocol; only warnings and errors may be emitted. */
#define ALOGE(...) fprintf(stderr, __VA_ARGS__)
#define ALOGW(...) fprintf(stderr, __VA_ARGS__)
#define ALOGD(...) ((void)0)
#define ALOGI(...) ((void)0)
#define ALOGV(...) ((void)0)
#define SLOGE(...) fprintf(stderr, __VA_ARGS__)
#define SLOGW(...) fprintf(stderr, __VA_ARGS__)
#define SLOGD(...) ((void)0)
#define SLOGI(...) ((void)0)
#define SLOGV(...) ((void)0)

#define CONDITION(cond) (__builtin_expect((cond) != 0, 0))
#define LOG_ALWAYS_FATAL_IF(cond, ...) \
  ((CONDITION(cond)) ? ((void)ALOGE(__VA_ARGS__)) : (void)0)
#define LOG_FATAL_IF(cond, ...) LOG_ALWAYS_FATAL_IF(cond, ##__VA_ARGS__)
#define ALOG_ASSERT(cond, ...) LOG_FATAL_IF(!(cond), ##__VA_ARGS__)
#define LOG_ALWAYS_FATAL(...) ((void)ALOGE(__VA_ARGS__))
#define ALOGW_IF(cond, ...) \
  ((CONDITION(cond)) ? ((void)ALOGW(__VA_ARGS__)) : (void)0)
#define LOG_EVENT_INT(tag, value) ((void)0)

#endif  // PINOZCAM_VENDOR_LOG_H_
